package capability

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"
)

const (
	defaultMaxFileBytes    = 512 << 10
	defaultMaxFiles        = 50_000
	defaultGitOutputBytes  = 8 << 20
	repositoryHeadFilename = ".prd-agent-head.json"
)

var (
	repositoryPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
	revisionPattern   = regexp.MustCompile(`^[0-9a-f]{40}$`)
)

var ErrRevisionUnavailable = errors.New("repository revision is unavailable locally")

type RepositoryConfig struct {
	Repository       string
	Root             string
	Token            string
	RemoteURL        string
	AllowLocalRemote bool
	GitBinary        string
	MaxFileBytes     int64
	MaxFiles         int
}

type RepositoryHead struct {
	Branch      string    `json:"branch"`
	Revision    string    `json:"revision"`
	RefreshedAt time.Time `json:"refreshed_at"`
}

type RepositoryManager struct {
	repository   string
	root         string
	mirror       string
	remoteURL    string
	token        string
	gitBinary    string
	maxFileBytes int64
	maxFiles     int
	mu           sync.RWMutex
}

type SearchHit struct {
	Path    string
	Line    int
	Snippet string
	score   int
}

type File struct {
	Path        string
	Content     []byte
	ContentHash string
}

type repositoryBackend int

const (
	backendGit repositoryBackend = iota + 1
	backendLegacySnapshot
)

func NewRepositoryManager(cfg RepositoryConfig) (*RepositoryManager, error) {
	repository := strings.TrimSpace(cfg.Repository)
	if !repositoryPattern.MatchString(repository) {
		return nil, fmt.Errorf("repository must use owner/name syntax")
	}
	root := filepath.Clean(strings.TrimSpace(cfg.Root))
	if root == "." || !filepath.IsAbs(root) {
		return nil, fmt.Errorf("repository root must be an absolute path")
	}
	remoteURL := strings.TrimSpace(cfg.RemoteURL)
	if remoteURL == "" {
		remoteURL = "https://github.com/" + repository + ".git"
	} else if !cfg.AllowLocalRemote {
		return nil, fmt.Errorf("custom repository remote is allowed only for local tests")
	}
	gitBinary := strings.TrimSpace(cfg.GitBinary)
	if gitBinary == "" {
		gitBinary = "git"
	}
	if _, err := exec.LookPath(gitBinary); err != nil {
		return nil, fmt.Errorf("git executable is required: %w", err)
	}
	return &RepositoryManager{
		repository:   repository,
		root:         root,
		mirror:       filepath.Join(root, "mirror.git"),
		remoteURL:    remoteURL,
		token:        strings.TrimSpace(cfg.Token),
		gitBinary:    gitBinary,
		maxFileBytes: positiveInt64(cfg.MaxFileBytes, defaultMaxFileBytes),
		maxFiles:     positiveInt(cfg.MaxFiles, defaultMaxFiles),
	}, nil
}

// Refresh resolves GitHub's default branch and fetches only that branch into a
// persistent bare mirror. Git transfers only objects missing from the mirror.
// The published local HEAD marker is replaced only after the fetched commit is
// fully available.
func (m *RepositoryManager) Refresh(ctx context.Context) (RepositoryHead, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if err := m.ensureMirrorLocked(ctx); err != nil {
		return RepositoryHead{}, err
	}
	output, err := m.runGit(ctx, defaultGitOutputBytes,
		"--git-dir", m.mirror, "ls-remote", "--symref", "origin", "HEAD")
	if err != nil {
		return RepositoryHead{}, fmt.Errorf("resolve GitHub default branch: %w", err)
	}
	branch, advertisedRevision, err := parseRemoteHead(output)
	if err != nil {
		return RepositoryHead{}, err
	}
	remoteRef := "refs/heads/" + branch
	localRef := "refs/remotes/origin/" + branch
	if _, err := m.runGit(ctx, defaultGitOutputBytes,
		"--git-dir", m.mirror,
		"fetch", "--no-tags", "--no-write-fetch-head",
		"origin", "+"+remoteRef+":"+localRef); err != nil {
		return RepositoryHead{}, fmt.Errorf("incrementally fetch GitHub default branch: %w", err)
	}
	fetched, err := m.runGit(ctx, 1024,
		"--git-dir", m.mirror, "rev-parse", "--verify", localRef+"^{commit}")
	if err != nil {
		return RepositoryHead{}, fmt.Errorf("verify fetched GitHub head: %w", err)
	}
	revision := strings.TrimSpace(string(fetched))
	if !revisionPattern.MatchString(revision) {
		return RepositoryHead{}, fmt.Errorf("fetched GitHub head is not a full commit SHA")
	}
	if revision != advertisedRevision {
		// The branch may advance between ls-remote and fetch. The fetched ref is
		// still a complete, provider-returned default-branch commit and the next
		// refresh will converge again.
		advertisedRevision = revision
	}
	if !m.gitHasRevisionLocked(ctx, advertisedRevision) {
		return RepositoryHead{}, fmt.Errorf("fetched GitHub head is not available in the local mirror")
	}
	head := RepositoryHead{
		Branch:      branch,
		Revision:    advertisedRevision,
		RefreshedAt: time.Now().UTC(),
	}
	if err := m.writeHeadLocked(head); err != nil {
		return RepositoryHead{}, err
	}
	return head, nil
}

// LocalHead returns the last fully published mirror HEAD. The configured
// fallback revision is accepted only when its Git objects or a legacy
// filesystem snapshot are already present locally; it never performs network
// I/O.
func (m *RepositoryManager) LocalHead(ctx context.Context, fallbackRevision string) (RepositoryHead, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	var head RepositoryHead
	raw, err := os.ReadFile(filepath.Join(m.root, repositoryHeadFilename))
	if err == nil && json.Unmarshal(raw, &head) == nil &&
		revisionPattern.MatchString(head.Revision) &&
		(m.gitHasRevisionLocked(ctx, head.Revision) || snapshotReady(filepath.Join(m.root, head.Revision), head.Revision)) {
		return head, nil
	}
	fallbackRevision = strings.ToLower(strings.TrimSpace(fallbackRevision))
	if revisionPattern.MatchString(fallbackRevision) &&
		(m.gitHasRevisionLocked(ctx, fallbackRevision) || snapshotReady(filepath.Join(m.root, fallbackRevision), fallbackRevision)) {
		return RepositoryHead{Branch: "local-fallback", Revision: fallbackRevision}, nil
	}
	return RepositoryHead{}, ErrRevisionUnavailable
}

// Ensure verifies that a revision can be served from the local mirror or a
// legacy extracted snapshot. It refreshes the default branch once on a cache
// miss, then falls back to local files if the network refresh fails.
func (m *RepositoryManager) Ensure(ctx context.Context, revision string) (string, error) {
	backend, path, err := m.locateRevision(ctx, revision, true)
	if err != nil {
		return "", err
	}
	if backend == backendGit {
		return m.mirror, nil
	}
	return path, nil
}

func (m *RepositoryManager) Search(ctx context.Context, revision, query string, limit int) ([]SearchHit, error) {
	if limit < 1 || limit > 100 {
		return nil, fmt.Errorf("search limit must be between 1 and 100")
	}
	tokens := queryTokens(query)
	if len(tokens) == 0 {
		return nil, fmt.Errorf("repository query is empty")
	}
	backend, path, err := m.locateRevision(ctx, revision, true)
	if err != nil {
		return nil, err
	}
	if backend == backendLegacySnapshot {
		return m.searchLegacy(ctx, path, tokens, limit)
	}
	return m.searchGit(ctx, revision, tokens, limit)
}

func (m *RepositoryManager) Read(ctx context.Context, revision, requestedPath string) (File, error) {
	backend, root, err := m.locateRevision(ctx, revision, true)
	if err != nil {
		return File{}, err
	}
	cleanPath, err := cleanRepositoryPath(requestedPath)
	if err != nil {
		return File{}, err
	}
	if backend == backendLegacySnapshot {
		return m.readLegacy(root, cleanPath)
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.readGitLocked(ctx, revision, cleanPath)
}

func (m *RepositoryManager) Tree(ctx context.Context, revision, prefix string) ([]string, error) {
	backend, root, err := m.locateRevision(ctx, revision, true)
	if err != nil {
		return nil, err
	}
	cleanPrefix := ""
	if strings.TrimSpace(prefix) != "" {
		cleanPrefix, err = cleanRepositoryPath(prefix)
		if err != nil {
			return nil, err
		}
	}
	if backend == backendLegacySnapshot {
		return m.treeLegacy(ctx, root, cleanPrefix)
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	args := []string{"--git-dir", m.mirror, "ls-tree", "-r", "-z", "--name-only", revision}
	if cleanPrefix != "" {
		args = append(args, "--", ":(literal)"+cleanPrefix)
	}
	output, err := m.runGit(ctx, defaultGitOutputBytes, args...)
	if err != nil {
		return nil, fmt.Errorf("read repository tree from local mirror: %w", err)
	}
	paths := make([]string, 0, 256)
	for _, raw := range bytes.Split(output, []byte{0}) {
		if len(raw) == 0 {
			continue
		}
		path := string(raw)
		if ignoredRepositoryPath(path) {
			continue
		}
		paths = append(paths, path)
		if len(paths) > m.maxFiles {
			return nil, fmt.Errorf("repository tree exceeds file limit")
		}
	}
	sort.Strings(paths)
	return paths, nil
}

func (m *RepositoryManager) locateRevision(ctx context.Context, revision string, refreshOnMiss bool) (repositoryBackend, string, error) {
	revision = strings.ToLower(strings.TrimSpace(revision))
	if !revisionPattern.MatchString(revision) {
		return 0, "", fmt.Errorf("repository revision must be a full lowercase commit SHA")
	}
	m.mu.RLock()
	if m.gitHasRevisionLocked(ctx, revision) {
		m.mu.RUnlock()
		return backendGit, m.mirror, nil
	}
	legacy := filepath.Join(m.root, revision)
	if snapshotReady(legacy, revision) {
		m.mu.RUnlock()
		return backendLegacySnapshot, legacy, nil
	}
	m.mu.RUnlock()
	var refreshErr error
	if refreshOnMiss {
		_, refreshErr = m.Refresh(ctx)
		m.mu.RLock()
		if m.gitHasRevisionLocked(ctx, revision) {
			m.mu.RUnlock()
			return backendGit, m.mirror, nil
		}
		if snapshotReady(legacy, revision) {
			m.mu.RUnlock()
			return backendLegacySnapshot, legacy, nil
		}
		m.mu.RUnlock()
	}
	if refreshErr != nil {
		return 0, "", fmt.Errorf("%w: refresh failed and no local copy exists: %v", ErrRevisionUnavailable, refreshErr)
	}
	return 0, "", ErrRevisionUnavailable
}

func (m *RepositoryManager) ensureMirrorLocked(ctx context.Context) error {
	if info, err := os.Stat(m.mirror); err == nil {
		if !info.IsDir() {
			return fmt.Errorf("repository mirror path is not a directory")
		}
		if _, err := m.runGit(ctx, 1024, "--git-dir", m.mirror, "rev-parse", "--is-bare-repository"); err != nil {
			return fmt.Errorf("validate local repository mirror: %w", err)
		}
		output, err := m.runGit(ctx, 4096, "--git-dir", m.mirror, "remote", "get-url", "origin")
		if err != nil {
			return fmt.Errorf("read local mirror origin: %w", err)
		}
		if strings.TrimSpace(string(output)) != m.remoteURL {
			return fmt.Errorf("local repository mirror origin does not match configured repository")
		}
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.MkdirAll(m.root, 0o700); err != nil {
		return fmt.Errorf("create repository cache root: %w", err)
	}
	temporary, err := os.MkdirTemp(m.root, ".mirror-")
	if err != nil {
		return fmt.Errorf("create temporary repository mirror: %w", err)
	}
	defer os.RemoveAll(temporary)
	if _, err := m.runGit(ctx, 4096, "init", "--bare", temporary); err != nil {
		return fmt.Errorf("initialize bare repository mirror: %w", err)
	}
	if _, err := m.runGit(ctx, 4096, "--git-dir", temporary, "remote", "add", "origin", m.remoteURL); err != nil {
		return fmt.Errorf("configure repository mirror origin: %w", err)
	}
	if _, err := m.runGit(ctx, 4096, "--git-dir", temporary, "config", "gc.auto", "0"); err != nil {
		return fmt.Errorf("disable automatic repository object pruning: %w", err)
	}
	if err := os.Rename(temporary, m.mirror); err != nil {
		return fmt.Errorf("publish bare repository mirror: %w", err)
	}
	return nil
}

func (m *RepositoryManager) gitHasRevisionLocked(ctx context.Context, revision string) bool {
	if !revisionPattern.MatchString(revision) {
		return false
	}
	if _, err := os.Stat(filepath.Join(m.mirror, "HEAD")); err != nil {
		return false
	}
	_, err := m.runGit(ctx, 1024,
		"--git-dir", m.mirror, "cat-file", "-e", revision+"^{commit}")
	return err == nil
}

func (m *RepositoryManager) writeHeadLocked(head RepositoryHead) error {
	raw, err := json.Marshal(head)
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(m.root, ".head-")
	if err != nil {
		return fmt.Errorf("create repository HEAD marker: %w", err)
	}
	name := temporary.Name()
	defer os.Remove(name)
	if err := temporary.Chmod(0o600); err != nil {
		temporary.Close()
		return err
	}
	if _, err := temporary.Write(append(raw, '\n')); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(name, filepath.Join(m.root, repositoryHeadFilename)); err != nil {
		return fmt.Errorf("publish repository HEAD marker: %w", err)
	}
	return nil
}

func (m *RepositoryManager) readGitLocked(ctx context.Context, revision, path string) (File, error) {
	object := revision + ":" + path
	sizeOutput, err := m.runGit(ctx, 1024,
		"--git-dir", m.mirror, "cat-file", "-s", object)
	if err != nil {
		return File{}, os.ErrNotExist
	}
	size, err := strconv.ParseInt(strings.TrimSpace(string(sizeOutput)), 10, 64)
	if err != nil || size < 0 || size > m.maxFileBytes {
		return File{}, fmt.Errorf("repository file is not a bounded regular file")
	}
	content, err := m.runGit(ctx, int(size)+1,
		"--git-dir", m.mirror, "cat-file", "blob", object)
	if err != nil {
		return File{}, fmt.Errorf("read repository file from local mirror: %w", err)
	}
	digest := sha256.Sum256(content)
	return File{
		Path:        path,
		Content:     content,
		ContentHash: "sha256:" + hex.EncodeToString(digest[:]),
	}, nil
}

func (m *RepositoryManager) searchGit(ctx context.Context, revision string, tokens []string, limit int) ([]SearchHit, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	args := []string{"--git-dir", m.mirror, "grep", "-n", "-z", "-I", "-i", "-F"}
	for _, token := range tokens {
		args = append(args, "-e", token)
	}
	args = append(args, revision, "--")
	output, exitCode, err := m.runGitWithExit(ctx, defaultGitOutputBytes, args...)
	if err != nil && exitCode != 1 {
		return nil, fmt.Errorf("search local repository mirror: %w", err)
	}
	if exitCode == 1 || len(output) == 0 {
		return []SearchHit{}, nil
	}
	type rawHit struct {
		path  string
		line  int
		score int
	}
	rawHits := make([]rawHit, 0, limit)
	prefix := revision + ":"
	for len(output) > 0 {
		pathEnd := bytes.IndexByte(output, 0)
		if pathEnd < 0 {
			return nil, fmt.Errorf("parse git grep path")
		}
		path := strings.TrimPrefix(string(output[:pathEnd]), prefix)
		output = output[pathEnd+1:]
		lineEnd := bytes.IndexByte(output, 0)
		if lineEnd < 0 {
			return nil, fmt.Errorf("parse git grep line")
		}
		lineNumber, parseErr := strconv.Atoi(string(output[:lineEnd]))
		if parseErr != nil || lineNumber < 1 {
			return nil, fmt.Errorf("parse git grep line number")
		}
		output = output[lineEnd+1:]
		contentEnd := bytes.IndexByte(output, '\n')
		if contentEnd < 0 {
			contentEnd = len(output)
		}
		line := strings.ToLower(string(output[:contentEnd]))
		if contentEnd < len(output) {
			output = output[contentEnd+1:]
		} else {
			output = nil
		}
		if ignoredRepositoryPath(path) {
			continue
		}
		score := matchScore(line, tokens)*10 + matchScore(strings.ToLower(path), tokens)
		rawHits = append(rawHits, rawHit{path: path, line: lineNumber, score: score})
	}
	contentCache := make(map[string][]string)
	hits := make([]SearchHit, 0, len(rawHits))
	for _, raw := range rawHits {
		lines, ok := contentCache[raw.path]
		if !ok {
			file, readErr := m.readGitLocked(ctx, revision, raw.path)
			if readErr != nil || !textContent(file.Content) {
				continue
			}
			lines = strings.Split(string(file.Content), "\n")
			contentCache[raw.path] = lines
		}
		index := raw.line - 1
		if index < 0 || index >= len(lines) {
			continue
		}
		start := max(0, index-2)
		end := min(len(lines), index+3)
		snippet := strings.Join(lines[start:end], "\n")
		if len(snippet) > 4000 {
			snippet = snippet[:4000]
		}
		hits = append(hits, SearchHit{
			Path: raw.path, Line: raw.line, Snippet: snippet, score: raw.score,
		})
	}
	sortSearchHits(hits)
	if len(hits) > limit {
		hits = hits[:limit]
	}
	return hits, nil
}

func (m *RepositoryManager) readLegacy(root, cleanPath string) (File, error) {
	path := filepath.Join(root, filepath.FromSlash(cleanPath))
	info, err := os.Stat(path)
	if err != nil {
		return File{}, err
	}
	if !info.Mode().IsRegular() || info.Size() > m.maxFileBytes {
		return File{}, fmt.Errorf("repository file is not a bounded regular file")
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return File{}, err
	}
	digest := sha256.Sum256(content)
	return File{
		Path: cleanPath, Content: content,
		ContentHash: "sha256:" + hex.EncodeToString(digest[:]),
	}, nil
}

func (m *RepositoryManager) searchLegacy(ctx context.Context, root string, tokens []string, limit int) ([]SearchHit, error) {
	hits := make([]SearchHit, 0, limit)
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if entry.IsDir() {
			if path != root && ignoredDirectory(entry.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		if entry.Name() == ".prd-agent-revision" {
			return nil
		}
		info, err := entry.Info()
		if err != nil || info.Size() > m.maxFileBytes {
			return err
		}
		content, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if !textContent(content) {
			return nil
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		pathScore := matchScore(strings.ToLower(relative), tokens)
		lines := strings.Split(string(content), "\n")
		for index, line := range lines {
			score := matchScore(strings.ToLower(line), tokens)
			if score == 0 && pathScore == 0 {
				continue
			}
			start := max(0, index-2)
			end := min(len(lines), index+3)
			snippet := strings.Join(lines[start:end], "\n")
			if len(snippet) > 4000 {
				snippet = snippet[:4000]
			}
			hits = append(hits, SearchHit{
				Path: relative, Line: index + 1, Snippet: snippet,
				score: score*10 + pathScore,
			})
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("search local repository snapshot: %w", err)
	}
	sortSearchHits(hits)
	if len(hits) > limit {
		hits = hits[:limit]
	}
	return hits, nil
}

func (m *RepositoryManager) treeLegacy(ctx context.Context, root, cleanPrefix string) ([]string, error) {
	paths := make([]string, 0, 256)
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if entry.IsDir() {
			if path != root && ignoredDirectory(entry.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		if relative == ".prd-agent-revision" ||
			(cleanPrefix != "" && relative != cleanPrefix && !strings.HasPrefix(relative, cleanPrefix+"/")) {
			return nil
		}
		paths = append(paths, relative)
		if len(paths) > m.maxFiles {
			return fmt.Errorf("repository tree exceeds file limit")
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Strings(paths)
	return paths, nil
}

func (m *RepositoryManager) runGit(ctx context.Context, limit int, args ...string) ([]byte, error) {
	output, _, err := m.runGitWithExit(ctx, limit, args...)
	return output, err
}

func (m *RepositoryManager) runGitWithExit(ctx context.Context, limit int, args ...string) ([]byte, int, error) {
	command := exec.CommandContext(ctx, m.gitBinary, args...)
	command.Env = gitEnvironment(m.token)
	stdout := &boundedBuffer{limit: limit}
	stderr := &boundedBuffer{limit: 64 << 10}
	command.Stdout = stdout
	command.Stderr = stderr
	err := command.Run()
	if stdout.exceeded {
		return nil, -1, fmt.Errorf("git output exceeds limit")
	}
	if err == nil {
		return stdout.Bytes(), 0, nil
	}
	exitCode := -1
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		exitCode = exitError.ExitCode()
	}
	message := strings.TrimSpace(stderr.String())
	if message == "" {
		message = err.Error()
	}
	return stdout.Bytes(), exitCode, fmt.Errorf("git command failed: %s", message)
}

func parseRemoteHead(output []byte) (string, string, error) {
	var branch, revision string
	for _, line := range strings.Split(strings.TrimSpace(string(output)), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 3 && fields[0] == "ref:" && fields[2] == "HEAD" &&
			strings.HasPrefix(fields[1], "refs/heads/") {
			branch = strings.TrimPrefix(fields[1], "refs/heads/")
		}
		if len(fields) == 2 && fields[1] == "HEAD" && revisionPattern.MatchString(fields[0]) {
			revision = fields[0]
		}
	}
	if branch == "" || revision == "" || strings.ContainsAny(branch, "\x00\r\n") {
		return "", "", fmt.Errorf("GitHub did not advertise a valid default branch HEAD")
	}
	return branch, revision, nil
}

func gitEnvironment(token string) []string {
	env := make([]string, 0, len(os.Environ())+8)
	for _, value := range os.Environ() {
		if strings.HasPrefix(value, "GIT_CONFIG_COUNT=") ||
			strings.HasPrefix(value, "GIT_CONFIG_KEY_") ||
			strings.HasPrefix(value, "GIT_CONFIG_VALUE_") {
			continue
		}
		env = append(env, value)
	}
	env = append(env,
		"GIT_TERMINAL_PROMPT=0",
		"GIT_CONFIG_NOSYSTEM=1",
		"GIT_CONFIG_GLOBAL=/dev/null",
		"LC_ALL=C",
	)
	if token != "" {
		env = append(env,
			"GIT_CONFIG_COUNT=1",
			"GIT_CONFIG_KEY_0=http.https://github.com/.extraHeader",
			"GIT_CONFIG_VALUE_0=Authorization: Bearer "+token,
		)
	}
	return env
}

func snapshotReady(target, revision string) bool {
	value, err := os.ReadFile(filepath.Join(target, ".prd-agent-revision"))
	return err == nil && strings.TrimSpace(string(value)) == revision
}

func cleanRepositoryPath(requested string) (string, error) {
	clean := filepath.Clean(filepath.FromSlash(strings.TrimSpace(requested)))
	if clean == "." || filepath.IsAbs(clean) || clean == ".." ||
		strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("repository path is outside the snapshot")
	}
	return filepath.ToSlash(clean), nil
}

func queryTokens(query string) []string {
	fields := strings.FieldsFunc(strings.ToLower(query), func(value rune) bool {
		return !unicode.IsLetter(value) && !unicode.IsDigit(value) && value != '_' && value != '-'
	})
	seen := map[string]struct{}{}
	tokens := make([]string, 0, len(fields))
	for _, field := range fields {
		if utf8.RuneCountInString(field) < 2 {
			continue
		}
		if _, exists := seen[field]; exists {
			continue
		}
		seen[field] = struct{}{}
		tokens = append(tokens, field)
	}
	return tokens
}

func matchScore(value string, tokens []string) int {
	score := 0
	for _, token := range tokens {
		if strings.Contains(value, token) {
			score++
		}
	}
	return score
}

func sortSearchHits(hits []SearchHit) {
	sort.Slice(hits, func(i, j int) bool {
		if hits[i].score != hits[j].score {
			return hits[i].score > hits[j].score
		}
		if hits[i].Path != hits[j].Path {
			return hits[i].Path < hits[j].Path
		}
		return hits[i].Line < hits[j].Line
	})
}

func textContent(content []byte) bool {
	sample := content
	if len(sample) > 8192 {
		sample = sample[:8192]
	}
	if !utf8.Valid(sample) {
		return false
	}
	for _, value := range sample {
		if value == 0 {
			return false
		}
	}
	return true
}

func ignoredDirectory(name string) bool {
	switch name {
	case ".git", "node_modules", ".next", "vendor", "__pycache__", ".pytest_cache":
		return true
	default:
		return false
	}
}

func ignoredRepositoryPath(path string) bool {
	for _, component := range strings.Split(filepath.ToSlash(path), "/") {
		if ignoredDirectory(component) {
			return true
		}
	}
	return false
}

type boundedBuffer struct {
	bytes.Buffer
	limit    int
	exceeded bool
}

func (b *boundedBuffer) Write(value []byte) (int, error) {
	if b.limit < 1 || b.Len()+len(value) <= b.limit {
		return b.Buffer.Write(value)
	}
	remaining := b.limit - b.Len()
	if remaining > 0 {
		_, _ = b.Buffer.Write(value[:remaining])
	}
	b.exceeded = true
	return len(value), io.ErrShortWrite
}

func positiveInt(value, fallback int) int {
	if value > 0 {
		return value
	}
	return fallback
}

func positiveInt64(value, fallback int64) int64 {
	if value > 0 {
		return value
	}
	return fallback
}
