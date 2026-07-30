package capability

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRepositoryManagerIncrementallyRefreshesDefaultBranchAndReadsOldCommits(t *testing.T) {
	remote, work := repositoryFixture(t)
	writeRepositoryFile(t, work, "README.md", "# PRD Agent\n")
	writeRepositoryFile(t, work, "backend-go/cmd/api/main.go", "package main\n\ntype ControlPlane struct{}\n")
	firstRevision := commitAndPush(t, work, "initial")

	manager, err := NewRepositoryManager(RepositoryConfig{
		Repository:       "dante26979-droid/PRD-Agent",
		Root:             t.TempDir(),
		RemoteURL:        remote,
		AllowLocalRemote: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	firstHead, err := manager.Refresh(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if firstHead.Branch != "main" || firstHead.Revision != firstRevision {
		t.Fatalf("unexpected initial repository HEAD: %+v", firstHead)
	}

	hits, err := manager.Search(context.Background(), firstRevision, "ControlPlane", 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 1 || hits[0].Path != "backend-go/cmd/api/main.go" ||
		!strings.Contains(hits[0].Snippet, "ControlPlane") {
		t.Fatalf("unexpected repository search hits: %+v", hits)
	}
	file, err := manager.Read(context.Background(), firstRevision, "backend-go/cmd/api/main.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(file.Content), "type ControlPlane") ||
		!strings.HasPrefix(file.ContentHash, "sha256:") {
		t.Fatalf("unexpected repository file: %+v", file)
	}
	paths, err := manager.Tree(context.Background(), firstRevision, "backend-go")
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) != 1 || paths[0] != "backend-go/cmd/api/main.go" {
		t.Fatalf("unexpected repository tree: %+v", paths)
	}

	writeRepositoryFile(t, work, "README.md", "# PRD Agent\n\nlatest merged code\n")
	writeRepositoryFile(t, work, "backend-go/internal/new.go", "package internal\n")
	secondRevision := commitAndPush(t, work, "latest merged change")
	secondHead, err := manager.Refresh(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if secondHead.Revision != secondRevision || secondHead.Revision == firstHead.Revision {
		t.Fatalf("repository HEAD did not advance: first=%+v second=%+v", firstHead, secondHead)
	}
	latest, err := manager.Read(context.Background(), secondRevision, "README.md")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(latest.Content), "latest merged code") {
		t.Fatalf("latest default branch content was not available: %s", latest.Content)
	}
	previous, err := manager.Read(context.Background(), firstRevision, "README.md")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(previous.Content), "latest merged code") {
		t.Fatalf("pinned old commit changed after refresh: %s", previous.Content)
	}
}

func TestRepositoryManagerFallsBackToLocalMirrorWhenFetchFails(t *testing.T) {
	remote, work := repositoryFixture(t)
	writeRepositoryFile(t, work, "README.md", "# cached repository\n")
	revision := commitAndPush(t, work, "cached")
	root := t.TempDir()
	manager, err := NewRepositoryManager(RepositoryConfig{
		Repository:       "dante26979-droid/PRD-Agent",
		Root:             root,
		RemoteURL:        remote,
		AllowLocalRemote: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := manager.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	offlineRemote := remote + ".offline"
	if err := os.Rename(remote, offlineRemote); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.Refresh(context.Background()); err == nil {
		t.Fatal("refresh should fail while the remote is unavailable")
	}
	head, err := manager.LocalHead(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if head.Revision != revision {
		t.Fatalf("local fallback selected the wrong revision: %+v", head)
	}
	file, err := manager.Read(context.Background(), revision, "README.md")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(file.Content), "cached repository") {
		t.Fatalf("local mirror fallback returned wrong content: %s", file.Content)
	}
}

func TestRepositoryManagerFallsBackToLegacyLocalSnapshotAndRejectsTraversal(t *testing.T) {
	revision := strings.Repeat("b", 40)
	root := t.TempDir()
	snapshot := filepath.Join(root, revision)
	if err := os.MkdirAll(snapshot, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(snapshot, ".prd-agent-revision"), []byte(revision), 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(snapshot, "README.md"), []byte("legacy local copy\n"), 0o400); err != nil {
		t.Fatal(err)
	}
	manager, err := NewRepositoryManager(RepositoryConfig{
		Repository:       "dante26979-droid/PRD-Agent",
		Root:             root,
		RemoteURL:        filepath.Join(t.TempDir(), "missing.git"),
		AllowLocalRemote: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	file, err := manager.Read(context.Background(), revision, "README.md")
	if err != nil || !strings.Contains(string(file.Content), "legacy local copy") {
		t.Fatalf("legacy local fallback failed: file=%+v error=%v", file, err)
	}
	if _, err := manager.Read(context.Background(), revision, "../secret"); err == nil {
		t.Fatal("repository traversal must be rejected")
	}
}

func TestRepositoryManagerAgainstConfiguredGitHubRepository(t *testing.T) {
	repository := os.Getenv("PRD_AGENT_TEST_GITHUB_REPOSITORY")
	expectedRevision := os.Getenv("PRD_AGENT_TEST_GITHUB_REVISION")
	if repository == "" {
		t.Skip("real GitHub repository test is not configured")
	}
	manager, err := NewRepositoryManager(RepositoryConfig{
		Repository: repository,
		Root:       t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}
	head, err := manager.Refresh(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if expectedRevision != "" && head.Revision != expectedRevision {
		t.Fatalf("real GitHub HEAD changed: got %s want %s", head.Revision, expectedRevision)
	}
	file, err := manager.Read(context.Background(), head.Revision, "README.md")
	if err != nil {
		t.Fatal(err)
	}
	if len(file.Content) == 0 || !strings.HasPrefix(file.ContentHash, "sha256:") {
		t.Fatalf("real GitHub README was not read: %+v", file)
	}
	hits, err := manager.Search(context.Background(), head.Revision, "PRD Agent", 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) == 0 {
		t.Fatal("real GitHub mirror returned no search hits")
	}
	started := time.Now()
	refreshed, err := manager.Refresh(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if refreshed.Revision != head.Revision {
		t.Fatalf("unchanged GitHub default branch moved during test: first=%s second=%s", head.Revision, refreshed.Revision)
	}
	t.Logf("unchanged real GitHub mirror refresh completed in %s", time.Since(started))
}

func repositoryFixture(t *testing.T) (string, string) {
	t.Helper()
	root := t.TempDir()
	remote := filepath.Join(root, "remote.git")
	work := filepath.Join(root, "work")
	runFixtureGit(t, root, "init", "--bare", remote)
	runFixtureGit(t, root, "init", "-b", "main", work)
	runFixtureGit(t, work, "config", "user.email", "tests@example.com")
	runFixtureGit(t, work, "config", "user.name", "Repository Tests")
	runFixtureGit(t, work, "remote", "add", "origin", remote)
	runFixtureGit(t, remote, "symbolic-ref", "HEAD", "refs/heads/main")
	return remote, work
}

func commitAndPush(t *testing.T, work, message string) string {
	t.Helper()
	runFixtureGit(t, work, "add", ".")
	runFixtureGit(t, work, "commit", "-m", message)
	runFixtureGit(t, work, "push", "origin", "main")
	return strings.TrimSpace(runFixtureGit(t, work, "rev-parse", "HEAD"))
}

func writeRepositoryFile(t *testing.T, work, name, content string) {
	t.Helper()
	path := filepath.Join(work, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func runFixtureGit(t *testing.T, directory string, args ...string) string {
	t.Helper()
	command := exec.Command("git", args...)
	command.Dir = directory
	command.Env = append(os.Environ(),
		"GIT_CONFIG_NOSYSTEM=1",
		"GIT_CONFIG_GLOBAL=/dev/null",
		"GIT_TERMINAL_PROMPT=0",
	)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v failed: %v\n%s", args, err, output)
	}
	return string(output)
}
