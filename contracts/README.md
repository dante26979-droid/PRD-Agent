# Cross-language contracts

`proto/agent/v1` is the only shared contract between the Go Control Plane and
the Python Agent Runtime. It contains the Agent Execution lease/write surface
and the Capability Gateway read surface.

Generate language bindings from this directory after installing `buf`,
`protoc-gen-go`, `protoc-gen-go-grpc`, and the Python gRPC plugins:

```bash
buf lint
buf generate
python -m grpc_tools.protoc -I proto \
  --python_out=gen/python --grpc_python_out=gen/python \
  proto/agent/v1/agent_execution.proto
python -m grpc_tools.protoc -I proto \
  --python_out=gen/python --grpc_python_out=gen/python \
  proto/agent/v1/capability_gateway.proto
python -m grpc_tools.protoc -I proto \
  --python_out=gen/python --grpc_python_out=gen/python \
  proto/agent/v1/agent_worker.proto
```

Generated files are build artifacts. They are not hand-edited and are not the
source of truth. The Go service implementation must reject stale
`fencing_token` values and the Gateway must validate capability scope before
calling Feishu or GitHub.
