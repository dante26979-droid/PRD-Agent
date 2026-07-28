FROM golang:1.26-alpine AS build

WORKDIR /src/backend-go
ARG GOPROXY=https://proxy.golang.org,direct
ARG GOSUMDB=sum.golang.org
COPY backend-go/go.mod ./
COPY backend-go/go.sum ./
COPY contracts /src/contracts
RUN GOPROXY="${GOPROXY}" GOSUMDB="${GOSUMDB}" go mod download
COPY backend-go/ .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/prd-agent-go-api ./cmd/api \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/prd-agent-go-migrate ./cmd/migrate \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/prd-agent-go-maintenance ./cmd/maintenance

FROM gcr.io/distroless/static-debian12:nonroot

WORKDIR /app
COPY --from=build /out/prd-agent-go-api /app/prd-agent-go-api
COPY --from=build /out/prd-agent-go-migrate /app/prd-agent-go-migrate
COPY --from=build /out/prd-agent-go-maintenance /app/prd-agent-go-maintenance
COPY --from=build /src/backend-go/db/migrations /app/db/migrations

EXPOSE 8080
ENTRYPOINT ["/app/prd-agent-go-api"]
