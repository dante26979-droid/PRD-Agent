.PHONY: fmt test vet build migrate contracts

fmt:
	gofmt -w $$(find . -name '*.go')

test:
	go test ./...

vet:
	go vet ./...

build:
	go build ./cmd/api ./cmd/migrate ./cmd/maintenance

migrate:
	go run ./cmd/migrate

contracts:
	cd ../contracts && buf lint && buf generate
