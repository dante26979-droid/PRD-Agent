package buildcontract

import (
	"os"
	"strings"
	"testing"
)

func TestGoContainerBuildTargetsTheRuntimeArchitecture(t *testing.T) {
	dockerfile, err := os.ReadFile("../../Dockerfile")
	if err != nil {
		t.Fatal(err)
	}
	contents := string(dockerfile)
	if strings.Contains(contents, "GOARCH=amd64") {
		t.Fatal("Dockerfile must not hard-code amd64 binaries into images for other architectures")
	}
	if !strings.Contains(contents, "TARGETARCH") {
		t.Fatal("Dockerfile must select GOARCH from Docker's target architecture")
	}
}
