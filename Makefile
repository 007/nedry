BINARY_NAME=nedry
VERSION?=$(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")
BUILD_TIME=$(shell date -u +"%Y-%m-%dT%H:%M:%SZ")
LDFLAGS=-ldflags "-s -w -X main.Version=$(VERSION) -X main.BuildTime=$(BUILD_TIME)"

.PHONY: all build clean linux-amd64 linux-arm64 darwin-arm64 docker

all: build

build:
	CGO_ENABLED=0 go build $(LDFLAGS) -o $(BINARY_NAME) ./cmd/nedry

# Cross-compilation targets
linux-amd64:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build $(LDFLAGS) -o $(BINARY_NAME)-linux-amd64 ./cmd/nedry

linux-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build $(LDFLAGS) -o $(BINARY_NAME)-linux-arm64 ./cmd/nedry

darwin-arm64:
	CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build $(LDFLAGS) -o $(BINARY_NAME)-darwin-arm64 ./cmd/nedry

# Build all platforms
release: linux-amd64 linux-arm64 darwin-arm64
	@echo "Built binaries for all platforms:"
	@ls -la $(BINARY_NAME)-*

# Docker build (multi-arch)
docker:
	docker build -t $(BINARY_NAME):$(VERSION) .

docker-multiarch:
	docker buildx build --platform linux/amd64,linux/arm64 -t $(BINARY_NAME):$(VERSION) .

clean:
	rm -f $(BINARY_NAME) $(BINARY_NAME)-linux-amd64 $(BINARY_NAME)-linux-arm64 $(BINARY_NAME)-darwin-arm64

test:
	go test -v ./...

fmt:
	go fmt ./...

vet:
	go vet ./...
