FROM golang:1.26-alpine AS build

WORKDIR /src/backend-go
ARG GOPROXY=https://proxy.golang.org,direct
ARG GOSUMDB=sum.golang.org
ARG TARGETOS=linux
ARG TARGETARCH
COPY backend-go/go.mod ./
COPY backend-go/go.sum ./
COPY contracts /src/contracts
RUN GOPROXY="${GOPROXY}" GOSUMDB="${GOSUMDB}" go mod download
COPY backend-go/ .
RUN runtime_arch="${TARGETARCH:-$(go env GOARCH)}" \
    && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${runtime_arch}" go build -trimpath -ldflags="-s -w" -o /out/prd-agent-go-api ./cmd/api \
    && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${runtime_arch}" go build -trimpath -ldflags="-s -w" -o /out/prd-agent-go-migrate ./cmd/migrate \
    && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${runtime_arch}" go build -trimpath -ldflags="-s -w" -o /out/prd-agent-go-maintenance ./cmd/maintenance \
    && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${runtime_arch}" go build -trimpath -ldflags="-s -w" -o /out/prd-agent-go-integration ./cmd/integration \
    && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${runtime_arch}" go build -trimpath -ldflags="-s -w" -o /out/prd-agent-go-capability ./cmd/capability

FROM alpine:3.23

WORKDIR /app
RUN apk add --no-cache git
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/prd-agent-go-api /app/prd-agent-go-api
COPY --from=build /out/prd-agent-go-migrate /app/prd-agent-go-migrate
COPY --from=build /out/prd-agent-go-maintenance /app/prd-agent-go-maintenance
COPY --from=build /out/prd-agent-go-integration /app/prd-agent-go-integration
COPY --from=build /out/prd-agent-go-capability /app/prd-agent-go-capability
COPY --from=build /src/backend-go/db/migrations /app/db/migrations
RUN mkdir -p /repository && chown 65532:65532 /repository

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/app/prd-agent-go-api"]
