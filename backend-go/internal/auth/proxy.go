package auth

import (
	"crypto/subtle"
	"fmt"
	"net/http"
	"regexp"
	"strings"
)

const (
	ProxySecretHeader       = "X-PRD-Proxy-Secret"
	AuthenticatedUserHeader = "X-PRD-Authenticated-User"
)

var proxyUserID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$`)

type ProxyResolver struct {
	secret  string
	tenant  string
	allowed map[string]struct{}
}

func NewProxyResolver(secret, tenant string, users []string) (*ProxyResolver, error) {
	if len(secret) < 32 {
		return nil, fmt.Errorf("proxy secret must contain at least 32 characters")
	}
	if strings.TrimSpace(tenant) == "" {
		return nil, fmt.Errorf("default tenant is required")
	}
	allowed := make(map[string]struct{}, len(users))
	for _, user := range users {
		user = strings.TrimSpace(user)
		if !proxyUserID.MatchString(user) {
			return nil, fmt.Errorf("allowed user id is invalid")
		}
		allowed[user] = struct{}{}
	}
	if len(allowed) == 0 {
		return nil, fmt.Errorf("at least one allowed user is required")
	}
	return &ProxyResolver{secret: secret, tenant: tenant, allowed: allowed}, nil
}

func (r *ProxyResolver) Resolve(request *http.Request) (string, string, error) {
	presented := request.Header.Get(ProxySecretHeader)
	if len(presented) != len(r.secret) ||
		subtle.ConstantTimeCompare([]byte(presented), []byte(r.secret)) != 1 {
		return "", "", fmt.Errorf("proxy identity is not authenticated")
	}
	user := strings.TrimSpace(request.Header.Get(AuthenticatedUserHeader))
	if !proxyUserID.MatchString(user) {
		return "", "", fmt.Errorf("proxy identity is not authenticated")
	}
	if _, ok := r.allowed[user]; !ok {
		return "", "", fmt.Errorf("proxy identity is not authenticated")
	}
	return r.tenant, user, nil
}
