package auth

import (
	"context"
	"fmt"
	"net/http"
	"strings"

	"github.com/coreos/go-oidc/v3/oidc"
)

type OIDCResolver struct {
	verifier *oidc.IDTokenVerifier
}

type claims struct {
	Subject  string `json:"sub"`
	TenantID string `json:"tenant_id"`
}

func NewOIDCResolver(ctx context.Context, issuer, audience string) (*OIDCResolver, error) {
	if issuer == "" || audience == "" {
		return nil, fmt.Errorf("OIDC issuer and audience are required")
	}
	provider, err := oidc.NewProvider(ctx, issuer)
	if err != nil {
		return nil, fmt.Errorf("discover OIDC provider: %w", err)
	}
	return &OIDCResolver{verifier: provider.Verifier(&oidc.Config{ClientID: audience})}, nil
}

func (r *OIDCResolver) Resolve(request *http.Request) (string, string, error) {
	header := request.Header.Get("Authorization")
	if !strings.HasPrefix(header, "Bearer ") {
		return "", "", fmt.Errorf("bearer token is required")
	}
	token := strings.TrimSpace(strings.TrimPrefix(header, "Bearer "))
	idToken, err := r.verifier.Verify(request.Context(), token)
	if err != nil {
		return "", "", fmt.Errorf("verify OIDC token: %w", err)
	}
	var value claims
	if err := idToken.Claims(&value); err != nil {
		return "", "", fmt.Errorf("decode OIDC claims: %w", err)
	}
	if value.Subject == "" || value.TenantID == "" {
		return "", "", fmt.Errorf("OIDC token must contain sub and tenant_id")
	}
	return value.TenantID, value.Subject, nil
}
