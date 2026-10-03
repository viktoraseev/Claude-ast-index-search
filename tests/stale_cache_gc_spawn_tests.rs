//! Native cache-state regression; this does not establish MCP equivalence.
//! Separate executable so a suspended fork cannot inherit other tests' locks.
#![cfg(unix)]

use ast_index::db;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::{net::UnixStream, process::CommandExt};
use std::process::Command;
use std::time::{Duration, SystemTime};

#[test]
fn inherited_layout_lock_defers_gc_until_child_executes() {
    const CHILD: &str = "AST_INDEX_GC_TEST_EXEC_CHILD";
    if std::env::var_os(CHILD).is_some() {
        return;
    }
    let base = tempfile::TempDir::new().unwrap();
    let cache = base.path().join("1ea5e");
    fs::create_dir(&cache).unwrap();
    let anchor = cache.join("index.db");
    fs::write(&anchor, b"synthetic cache activity").unwrap();
    let now = SystemTime::now();
    OpenOptions::new()
        .write(true)
        .open(anchor)
        .unwrap()
        .set_modified(now - db::STALE_CACHE_MAX_AGE - Duration::from_secs(1))
        .unwrap();
    let leases = base.path().join(".leases");
    fs::create_dir(&leases).unwrap();
    let layout = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(leases.join("layout.lock"))
        .unwrap();
    fs2::FileExt::lock_exclusive(&layout).unwrap();

    let (mut parent, gate) = UnixStream::pair().unwrap();
    parent
        .set_read_timeout(Some(Duration::from_secs(30)))
        .unwrap();
    let spawner = std::thread::spawn(move || {
        let fd = gate.as_raw_fd();
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "inherited_layout_lock_defers_gc_until_child_executes",
            ])
            .env(CHILD, "1");
        unsafe {
            command.pre_exec(move || {
                // Only async-signal-safe operations between fork and exec.
                let mut byte = 1_u8;
                if libc::write(fd, (&byte as *const u8).cast(), 1) != 1
                    || libc::read(fd, (&mut byte as *mut u8).cast(), 1) != 1
                {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        command.status().unwrap()
    });
    parent.read_exact(&mut [0]).unwrap();
    drop(layout);
    let deferred = db::gc_stale_caches_in(base.path(), None, db::STALE_CACHE_MAX_AGE, now).unwrap();
    let retained = cache.is_dir();
    parent.write_all(&[1]).unwrap();
    assert!(spawner.join().unwrap().success());
    assert_eq!(deferred, 0);
    assert!(retained);
    assert_eq!(
        db::gc_stale_caches_in(base.path(), None, db::STALE_CACHE_MAX_AGE, now).unwrap(),
        1
    );
    assert!(!cache.exists());
}
