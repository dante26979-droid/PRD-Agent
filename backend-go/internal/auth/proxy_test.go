package auth

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestProxyResolverAcceptsOnlySignedAllowlistedIdentity(t *testing.T) {
	resolver, err := NewProxyResolver("proxy-secret-with-at-least-32-bytes", "default", []string{"alice"})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	request.Header.Set(ProxySecretHeader, "proxy-secret-with-at-least-32-bytes")
	request.Header.Set(AuthenticatedUserHeader, "alice")
	request.Header.Set("X-User-ID", "mallory")
	request.Header.Set("X-Tenant-ID", "other")

	tenant, owner, err := resolver.Resolve(request)
	if err != nil {
		t.Fatal(err)
	}
	if tenant != "default" || owner != "alice" {
		t.Fatalf("unexpected principal: %s/%s", tenant, owner)
	}
}

func TestProxyResolverRejectsForgedOrUnknownIdentity(t *testing.T) {
	resolver, err := NewProxyResolver("proxy-secret-with-at-least-32-bytes", "default", []string{"alice"})
	if err != nil {
		t.Fatal(err)
	}
	for name, values := range map[string][2]string{
		"wrong secret": {"wrong-secret", "alice"},
		"unknown user": {"proxy-secret-with-at-least-32-bytes", "mallory"},
		"invalid user": {"proxy-secret-with-at-least-32-bytes", "../alice"},
	} {
		t.Run(name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
			request.Header.Set(ProxySecretHeader, values[0])
			request.Header.Set(AuthenticatedUserHeader, values[1])
			if _, _, err := resolver.Resolve(request); err == nil {
				t.Fatal("forged proxy identity was accepted")
			}
		})
	}
}
