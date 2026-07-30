package feishu

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var (
	tokenPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,200}$`)
	hostPattern  = regexp.MustCompile(`^[A-Za-z0-9.-]+$`)
)

type Config struct {
	AppID         string
	AppSecret     string
	DocumentHost  string
	WikiNodeToken string
	BaseURL       string
	Timeout       time.Duration
}

type Client struct {
	config Config
	http   *http.Client
}

type Result struct {
	SafeURL          string
	ProviderRevision string
}

type ProviderError struct {
	Code          string
	Retryable     bool
	ResultUnknown bool
}

func (e *ProviderError) Error() string { return "Feishu publish failed: " + e.Code }

func NewClient(cfg Config, httpClient *http.Client) (*Client, error) {
	if cfg.AppID == "" || cfg.AppSecret == "" {
		return nil, fmt.Errorf("Feishu app credentials are required")
	}
	if !hostPattern.MatchString(cfg.DocumentHost) || !tokenPattern.MatchString(cfg.WikiNodeToken) {
		return nil, fmt.Errorf("Feishu document host or fixed Wiki node token is invalid")
	}
	if cfg.BaseURL == "" {
		cfg.BaseURL = "https://open.feishu.cn/open-apis"
	}
	parsed, err := url.Parse(cfg.BaseURL)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.Path != "/open-apis" {
		return nil, fmt.Errorf("Feishu base URL must be an HTTPS /open-apis endpoint")
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 15 * time.Second
	}
	if httpClient == nil {
		httpClient = &http.Client{Timeout: cfg.Timeout}
	}
	return &Client{config: cfg, http: httpClient}, nil
}

func (c *Client) Publish(ctx context.Context, draft []byte) (Result, error) {
	markdown, err := draftMarkdown(draft)
	if err != nil {
		return Result{}, &ProviderError{Code: "INVALID_DRAFT"}
	}
	token, err := c.tenantToken(ctx)
	if err != nil {
		return Result{}, err
	}
	documentID, err := c.documentID(ctx, token)
	if err != nil {
		return Result{}, err
	}
	if _, err := c.metadata(ctx, token, documentID, false); err != nil {
		return Result{}, err
	}
	children, err := c.children(ctx, token, documentID, false)
	if err != nil {
		return Result{}, err
	}
	mutationStarted := false
	if children > 0 {
		mutationStarted = true
		path := fmt.Sprintf("/docx/v1/documents/%s/blocks/%s/children/batch_delete", url.PathEscape(documentID), url.PathEscape(documentID))
		if _, err := c.request(ctx, token, http.MethodDelete, path, map[string]any{"start_index": 0, "end_index": children}, true); err != nil {
			return Result{}, asMutationUnknown(err)
		}
	}
	blocks, err := renderMarkdown(markdown)
	if err != nil {
		if mutationStarted {
			return Result{}, &ProviderError{Code: "RESULT_UNKNOWN", ResultUnknown: true}
		}
		return Result{}, err
	}
	if len(blocks) > 0 {
		mutationStarted = true
		path := fmt.Sprintf("/docx/v1/documents/%s/blocks/%s/children", url.PathEscape(documentID), url.PathEscape(documentID))
		if _, err := c.request(ctx, token, http.MethodPost, path, map[string]any{"children": blocks}, true); err != nil {
			return Result{}, asMutationUnknown(err)
		}
	}
	revision, err := c.metadata(ctx, token, documentID, mutationStarted)
	if err != nil {
		return Result{}, asMutationUnknown(err)
	}
	return Result{
		SafeURL:          "https://" + c.config.DocumentHost + "/wiki/" + url.PathEscape(c.config.WikiNodeToken),
		ProviderRevision: revision,
	}, nil
}

func (c *Client) Reconcile(ctx context.Context, draft []byte) (Result, error) {
	markdown, err := draftMarkdown(draft)
	if err != nil {
		return Result{}, &ProviderError{Code: "INVALID_DRAFT"}
	}
	token, err := c.tenantToken(ctx)
	if err != nil {
		return Result{}, err
	}
	documentID, err := c.documentID(ctx, token)
	if err != nil {
		return Result{}, err
	}
	path := fmt.Sprintf("/docx/v1/documents/%s/raw_content", url.PathEscape(documentID))
	body, err := c.request(ctx, token, http.MethodGet, path, nil, false)
	if err != nil {
		return Result{}, err
	}
	data, _ := body["data"].(map[string]any)
	content, _ := data["content"].(string)
	if normalizedHash(content) != normalizedHash(markdown) {
		return Result{}, &ProviderError{Code: "MANUAL_REVIEW"}
	}
	revision, err := c.metadata(ctx, token, documentID, false)
	if err != nil {
		return Result{}, err
	}
	return Result{
		SafeURL:          "https://" + c.config.DocumentHost + "/wiki/" + url.PathEscape(c.config.WikiNodeToken),
		ProviderRevision: revision,
	}, nil
}

func (c *Client) tenantToken(ctx context.Context) (string, error) {
	body, err := c.request(ctx, "", http.MethodPost, "/auth/v3/tenant_access_token/internal", map[string]any{
		"app_id": c.config.AppID, "app_secret": c.config.AppSecret,
	}, false)
	if err != nil {
		return "", err
	}
	token, _ := body["tenant_access_token"].(string)
	if token == "" {
		return "", &ProviderError{Code: "INVALID_PROVIDER_RESPONSE"}
	}
	return token, nil
}

func (c *Client) documentID(ctx context.Context, token string) (string, error) {
	body, err := c.request(ctx, token, http.MethodGet, "/wiki/v2/spaces/get_node?token="+url.QueryEscape(c.config.WikiNodeToken), nil, false)
	if err != nil {
		return "", err
	}
	data, _ := body["data"].(map[string]any)
	node, _ := data["node"].(map[string]any)
	documentID, _ := node["obj_token"].(string)
	if node["obj_type"] != "docx" || !tokenPattern.MatchString(documentID) {
		return "", &ProviderError{Code: "INVALID_FIXED_WIKI_NODE"}
	}
	return documentID, nil
}

func (c *Client) metadata(ctx context.Context, token, documentID string, mutationStarted bool) (string, error) {
	body, err := c.request(ctx, token, http.MethodGet, "/docx/v1/documents/"+url.PathEscape(documentID), nil, mutationStarted)
	if err != nil {
		return "", err
	}
	data, _ := body["data"].(map[string]any)
	document, _ := data["document"].(map[string]any)
	switch value := document["revision_id"].(type) {
	case string:
		return value, nil
	case float64:
		return strconv.FormatInt(int64(value), 10), nil
	default:
		return "", &ProviderError{Code: "INVALID_PROVIDER_RESPONSE", ResultUnknown: mutationStarted}
	}
}

func (c *Client) children(ctx context.Context, token, documentID string, mutationStarted bool) (int, error) {
	path := fmt.Sprintf("/docx/v1/documents/%s/blocks/%s/children?page_size=500", url.PathEscape(documentID), url.PathEscape(documentID))
	body, err := c.request(ctx, token, http.MethodGet, path, nil, mutationStarted)
	if err != nil {
		return 0, err
	}
	data, _ := body["data"].(map[string]any)
	items, ok := data["items"].([]any)
	if !ok {
		return 0, &ProviderError{Code: "INVALID_PROVIDER_RESPONSE", ResultUnknown: mutationStarted}
	}
	return len(items), nil
}

func (c *Client) request(ctx context.Context, token, method, path string, payload any, mutationStarted bool) (map[string]any, error) {
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return nil, err
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, c.config.BaseURL+path, body)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json; charset=utf-8")
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return nil, &ProviderError{Code: "TRANSPORT", Retryable: !mutationStarted, ResultUnknown: mutationStarted}
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, 2<<20))
	if err != nil {
		return nil, &ProviderError{Code: "TRANSPORT", Retryable: !mutationStarted, ResultUnknown: mutationStarted}
	}
	var value map[string]any
	if json.Unmarshal(raw, &value) != nil {
		return nil, &ProviderError{Code: "INVALID_PROVIDER_RESPONSE", ResultUnknown: mutationStarted}
	}
	code, _ := value["code"].(float64)
	if code == 99991663 || code == 99991668 || code == 99991672 || response.StatusCode == 401 || response.StatusCode == 403 {
		return nil, &ProviderError{Code: "UNAUTHORIZED"}
	}
	if response.StatusCode == 429 {
		return nil, &ProviderError{Code: "RATE_LIMITED", Retryable: !mutationStarted, ResultUnknown: mutationStarted}
	}
	if response.StatusCode >= 500 {
		return nil, &ProviderError{Code: "PROVIDER_UNAVAILABLE", Retryable: !mutationStarted, ResultUnknown: mutationStarted}
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 || code != 0 {
		return nil, &ProviderError{Code: "PROVIDER_REJECTED", ResultUnknown: mutationStarted}
	}
	return value, nil
}

func draftMarkdown(draft []byte) (string, error) {
	var value struct {
		Markdown string `json:"markdown"`
	}
	if json.Unmarshal(draft, &value) == nil && strings.TrimSpace(value.Markdown) != "" {
		return value.Markdown, nil
	}
	if strings.TrimSpace(string(draft)) != "" && !json.Valid(draft) {
		return string(draft), nil
	}
	return "", errors.New("draft does not contain markdown")
}

func renderMarkdown(markdown string) ([]map[string]any, error) {
	lines := strings.Split(strings.ReplaceAll(markdown, "\r\n", "\n"), "\n")
	blocks := make([]map[string]any, 0, len(lines))
	inCode := false
	codeLines := make([]string, 0)
	flushCode := func() {
		if len(codeLines) > 0 {
			blocks = append(blocks, textBlock(14, "code", strings.Join(codeLines, "\n")))
			codeLines = codeLines[:0]
		}
	}
	for _, line := range lines {
		if strings.HasPrefix(strings.TrimSpace(line), "```") {
			if inCode {
				flushCode()
			}
			inCode = !inCode
			continue
		}
		if inCode {
			codeLines = append(codeLines, line)
			continue
		}
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			continue
		}
		blockType, key, content := 2, "text", trimmed
		switch {
		case strings.HasPrefix(trimmed, "### "):
			blockType, key, content = 5, "heading3", strings.TrimPrefix(trimmed, "### ")
		case strings.HasPrefix(trimmed, "## "):
			blockType, key, content = 4, "heading2", strings.TrimPrefix(trimmed, "## ")
		case strings.HasPrefix(trimmed, "# "):
			blockType, key, content = 3, "heading1", strings.TrimPrefix(trimmed, "# ")
		case strings.HasPrefix(trimmed, "- "):
			blockType, key, content = 12, "bullet", strings.TrimPrefix(trimmed, "- ")
		}
		blocks = append(blocks, textBlock(blockType, key, content))
		if len(blocks) > 500 {
			return nil, &ProviderError{Code: "DOCUMENT_TOO_LARGE"}
		}
	}
	if inCode {
		flushCode()
	}
	return blocks, nil
}

func textBlock(blockType int, key, content string) map[string]any {
	return map[string]any{
		"block_type": blockType,
		key: map[string]any{
			"elements": []any{map[string]any{"text_run": map[string]any{
				"content": content, "text_element_style": map[string]any{},
			}}},
			"style": map[string]any{},
		},
	}
}

func normalizedHash(value string) string {
	normalized := strings.Join(strings.Fields(value), " ")
	digest := sha256.Sum256([]byte(normalized))
	return fmt.Sprintf("%x", digest[:])
}

func asMutationUnknown(err error) error {
	var provider *ProviderError
	if errors.As(err, &provider) {
		provider.ResultUnknown = true
		provider.Retryable = false
		provider.Code = "RESULT_UNKNOWN"
		return provider
	}
	return &ProviderError{Code: "RESULT_UNKNOWN", ResultUnknown: true}
}
