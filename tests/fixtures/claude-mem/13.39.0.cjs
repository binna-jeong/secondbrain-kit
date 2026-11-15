// trimmed from claude-mem@13.39.0 plugin/scripts/worker-service.cjs (definitions + buildLauncherPrefix only)
var q1=1,Tv=["onnxruntime>=1.20","protobuf<7","chromadb==1.5.9","pydantic<2.14"],z2=2;var k0="0.2.6",y3=3;
class Gn{static x(){}static buildLauncherPrefix(e){let r=Tv.flatMap(n=>["--with",n]);return["--python",e,...r,"--from",`chroma-mcp==${k0}`,"chroma-mcp"]}static buildPrewarmCommandArgs(e){return e}}
