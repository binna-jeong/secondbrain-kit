// trimmed from claude-mem@13.34.2 plugin/scripts/worker-service.cjs (definitions + buildLauncherPrefix only)
var q1=1,BE=["onnxruntime>=1.20","protobuf<7","chromadb==1.5.9"],z2=2;var F0="0.2.6",y3=3;
class Gn{static x(){}static buildLauncherPrefix(e){let r=BE.flatMap(n=>["--with",n]);return["--python",e,...r,"--from",`chroma-mcp==${F0}`,"chroma-mcp"]}static buildPrewarmCommandArgs(e){return e}}
