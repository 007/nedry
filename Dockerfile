FROM golang:1.21-alpine AS builder
LABEL maintainer="ryan@espressive.com"

WORKDIR /build

# Copy go mod files first for better caching
COPY go.mod go.sum ./
RUN go mod download

# Copy source code
COPY cmd/ cmd/
COPY pkg/ pkg/

# Build static binary
RUN CGO_ENABLED=0 GOOS=linux go build -a -installsuffix cgo -o nedry ./cmd/nedry

# Final minimal distroless image
FROM gcr.io/distroless/static-debian12:nonroot

WORKDIR /app
COPY --from=builder /build/nedry /app/nedry

USER nonroot:nonroot

ENTRYPOINT ["/app/nedry"]
