"""Reviewed finite Java resource API obligations, independent of current verdicts."""
from pathlib import Path
import re

from common import stable_id
from scope_acceptance_spec import population_shape


def cli_surface():
    source = (Path(__file__).resolve().parents[2] / 'src/main.rs').read_text()
    cli = source.split('struct Cli {', 1)[1].split('\n}', 1)[0]
    commands = source.split('enum Commands {', 1)[1].split('\n}', 1)[0]
    blocks = {}
    for name in ('XmlUsages', 'ResourceUsages', 'UnusedDeps'):
        body = commands.split('    ' + name + ' {', 1)[1].split('\n    },', 1)[0]
        blocks[name] = re.sub(r'\s+', '', re.sub(r'^\s*//[^\n]*', '', body, flags=re.MULTILINE))
    blocks['Cli'] = re.sub(r'\s+', '', re.sub(r'^\s*//[^\n]*', '', cli, flags=re.MULTILINE))
    return stable_id(blocks)


def criteria():
    return [dict(feature=f,subject=s,samples_count=n,sample_keys_sha256=k,population_sha256=p,
                 fixture=m+'.exercise',production=path,contract=contract)
            for f,s,n,k,p,m,path,contract in RETAINED]


RETAINED = (
    ('resource-usages', 'disposable-java-android', 11, '211dcf7c5fcbb8af69f3b9181b0a9ce7e96b5a623b4156601d0d59dbf2321ed7', 'f647a72abc37c29e1292c5483adc79b9ab1f56cb3746f969393514f04f6a0969', 'android_contracts', 'src/commands/android.rs', '@string/shared, R.string.shared and bare shared; exact/missing module, type, true counts and ten-site caps, unused configuration variants and owner scope'),
    ('xml-usages', 'disposable-java-android', 7, '6d84bffd99426f84607cbdf07286facd61ccafd2ffccbe28f51d524bc310f272', 'f18c837a7deb6a8260ae07946105b478f3ffa6494678fe807ba788cfb7108aa6', 'android_contracts', 'src/commands/android.rs', 'Widget Java class layout references; exact/unowned/missing modules, zero matches and global versus selected-module hundred-site caps'),
    ('resource-usages:java-lexical', 'disposable-java-resource-syntax-v1', 4, '5e85e35380e114511f4c1e2ccb7e4a893f77e4147567ac9c56778ccf89fb5284', '6a8bee4567f5a656b46094fc30666583cf9051a0a9e35bde1d9cdad89a793d85', 'java_resource_contracts', 'src/parsers/treesitter/java.rs', 'direct, spaced and multiline R expressions with comment/string/platform/unknown and non-R negatives'),
    ('resource-usages:java-imports', 'disposable-java-resource-syntax-v1', 4, '3a6556dc8242e65b077ccc2fa056a809066f39960892e52455785fc4adf1182b', '0b287a22079af306770f51d927335cf7df6aeace482ad8fc7b3ba5ce0b05ae51', 'java_resource_contracts', 'src/parsers/treesitter/java.rs', 'explicit R, kind aliases, static single and wildcard constants, field/method namespace separation and ambiguous ownership'),
    ('resource-usages:java-namespace-literals', 'disposable-java-resource-syntax-v1', 3, '7515ab543875b37403df15421bb041598dea7a3838a733595f487c833fd2af8e', '13d7e717f901bb5f147a29ff06481a4593f552e46aac2fc3bedd851123f7419f', 'java_resource_contracts', 'src/indexer/java_resources.rs', 'same resource name in app/library; literal namespace declaring identity and unused-owner classification'),
    ('resource-usages:java-lexical-bindings', 'disposable-java-resource-lexical-bindings-v1', 55, '15f3f38db4d866f502d369bdfeb61dc3720e8afa4a2f4119bb00b4d0990b9161', 'f625bb5c410408e539628eeb2d2d0604ab5f3596294f07d1b24a45715c3fe2bd', 'java_resource_binding_contracts', 'src/commands/modules.rs java_resource_references', 'R and static fields at byte sites: source-classpath inherited/type/member/package/import precedence, local/field/type/pattern/loop/try/capture scopes and condition-exit continuations; javac and used/unused owners'),
    ('resource-usages:java-owner-resolution', 'disposable-java-resource-owner-resolution-v1', 34, '4487e519d2ca099a212429e1a3d775d80aeb9b5b64fc604413cb04e4426230b5', '162e2b6149ca6b0f475e93cd6f8eda429b07173009296113ebea4320580a75e2', 'java_resource_ownership_contracts', 'src/indexer/java_resources.rs and src/indexer.rs', 'explicit merged/nontransitive dependency R modes; literal/alias/manifest namespaces and unknown-computed guard; integer/bool/array/plurals definitions, indexed R and attached declaring roots with decoys'),
    ('resource-usages:java-namespace-ownership', 'disposable-java-android-dependency-ownership', 6, '6bb467ad5c8caa481bca3a55f985d387c67835cf3b478678f1154a248c8b3e82', '68ef7bc9f00e2b33ccde35b583f0c93586aa349fcfaa38bf820ca9de50d868ca', 'android_dependency_contracts', 'src/indexer.rs', 'qualified Java references, configuration deduplication, owner-unused classification and duplicate namespace guard; XML-only legacy criteria excluded'),
    ('unused-deps:java-android-ownership', 'disposable-java-android-dependency-ownership', 12, '8b3c7b819e98ed2f073d975f0d1f70cbcd7b652d0ae5e83e508e3b58fe468c2e', '5488cf2683acdeda0d432040b2de75e3a0608ab6a8cbaa87d8ddc10db94cca90', 'android_dependency_contracts', 'src/commands/modules.rs', 'Java class layout and resource declaring-module provenance; all XML/resource/verbose switch products, strict and no-transitive controls; other unused-deps semantics remain separate'),
    ('global:scope:java-resources', 'disposable-java-resource-scope', 382, '290846ebc42579b01b99cb543985a637a51798dfefa4cd8aa5457cae8f22a3bc', 'da6f373dae87def8a0d146a24f01231f352dc94ccdb56f9edca8fea7988fa82b', 'java_resource_scope_contracts', 'src/commands/android.rs', 'Java R and Widget layout sites in colliding roots: local/subtree/cwd/module/type intersections, selected counts before caps, unused definitions used outside cwd, refresh and detach'),
    ('resource-usages:java-definition-bindings', 'disposable-java-resource-definition-bindings-v1', 46, 'f562089d068557579308c12eb9dd3528d553f4ea0c9d88aa531ed56fcfc7d78d', '3cbe0cc319c245e4ab02c2fd39f14d960b544ad9576609d482b96d262a8c3fa7', 'java_resource_definition_contracts', 'src/indexer.rs and src/commands/modules.rs java_resource_references', 'all inventoried file-resource kinds including raw/font/nine-patch and configurations; indexed R public/package/protected/private/nest/type/nonstatic/missing-member guards validated by javac, update and unused ownership'),
    ('resource-usages:java-metadata-ownership', 'disposable-java-resource-metadata-ownership-v1', 15, '08b64cc4fab62670fe829ba8a8fd75aeb8e2e45e7fc6ab68b0c279398a9560ec', '8763c38bc521af24da41d296e83c7a8616ea525c10b84409f1133583530bd3e8', 'java_resource_metadata_contracts', 'src/indexer.rs and src/indexer/java_resources.rs', 'Java attr/id/styleable array/index declaring ownership, group isolation and unused names; source-proven concatenation/alias/parenthesized namespaces with unknown/reassigned/call guards and incremental refresh'),
)

REVIEWED_SURFACE_SHA256 = '1b9c831fe3f7fd385069ed6f043876ed2182ab65f6d528c50f1a0bb539e94402'
