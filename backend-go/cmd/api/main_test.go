package main

import (
	"io"
	"net/http"
	"strings"
	"testing"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func TestHealthcheckRequiresReadyResponse(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Status:     "200 OK",
			Body:       io.NopCloser(strings.NewReader("")),
		}, nil
	})}
	if err := checkHealth(client, "http://health/ready"); err != nil {
		t.Fatalf("ready endpoint was rejected: %v", err)
	}

	client.Transport = roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusServiceUnavailable,
			Status:     "503 Service Unavailable",
			Body:       io.NopCloser(strings.NewReader("")),
		}, nil
	})
	if err := checkHealth(client, "http://health/ready"); err == nil {
		t.Fatal("not-ready endpoint passed the container healthcheck")
	}
}
