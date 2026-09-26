package launch

import (
	"runtime"
	"slices"
	"testing"
)

func TestCometRegistryEntry(t *testing.T) {
	spec, err := LookupIntegrationSpec("comet")
	if err != nil {
		t.Fatalf("LookupIntegrationSpec(comet) failed: %v", err)
	}
	if spec.Name != "comet" {
		t.Errorf("Name = %q, want comet", spec.Name)
	}
	if spec.Runner.String() != "Perplexity Comet" {
		t.Errorf("String() = %q, want Perplexity Comet", spec.Runner.String())
	}
	if !slices.Contains(spec.Aliases, "perplexity") {
		t.Errorf("Aliases = %v, want to contain perplexity", spec.Aliases)
	}
	if spec.Hidden {
		t.Error("comet must not be hidden")
	}
	if !slices.Contains(launcherIntegrationOrder, "comet") {
		t.Error("comet missing from launcherIntegrationOrder")
	}
	if spec.Install.CheckInstalled == nil {
		t.Error("comet spec is missing CheckInstalled")
	}
	if spec.Install.EnsureInstalled == nil {
		t.Error("comet spec is missing EnsureInstalled")
	}
}

func TestChromeRegistryEntry(t *testing.T) {
	spec, err := LookupIntegrationSpec("chromium")
	if err != nil {
		t.Fatalf("LookupIntegrationSpec(chromium) failed: %v", err)
	}
	if spec.Name != "chrome" {
		t.Errorf("Name = %q, want chrome", spec.Name)
	}
	if spec.Runner.String() != "Chrome" {
		t.Errorf("String() = %q, want Chrome", spec.Runner.String())
	}
	if !slices.Contains(launcherIntegrationOrder, "chrome") {
		t.Error("chrome missing from launcherIntegrationOrder")
	}
	if spec.Hidden {
		t.Error("chrome must not be hidden")
	}
	if spec.Install.CheckInstalled == nil {
		t.Error("chrome spec is missing CheckInstalled")
	}
	if spec.Install.EnsureInstalled == nil {
		t.Error("chrome spec is missing EnsureInstalled")
	}
}

func TestCometAndChromeSupported(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("companion integration is Windows-only today")
	}
	if err := (&Comet{}).Supported(); err != nil {
		t.Errorf("Comet.Supported() = %v, want nil", err)
	}
	if err := (&Chrome{}).Supported(); err != nil {
		t.Errorf("Chrome.Supported() = %v, want nil", err)
	}
}

func TestCompanionLauncherMissing(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("companion paths are Windows-only today")
	}
	t.Setenv("LOCALAPPDATA", t.TempDir())
	if _, err := companionLauncher("comet"); err == nil {
		t.Error("companionLauncher(comet) succeeded in an empty companion dir, want error")
	}
	if _, err := companionLauncher("chrome"); err == nil {
		t.Error("companionLauncher(chrome) succeeded in an empty companion dir, want error")
	}
}

func TestCompanionArgsPowerShell(t *testing.T) {
	launcher := `C:\comet\Launch-OllamaComet.ps1`
	argv := companionArgs(launcher, "comet", "llama3", []string{"--debug"})
	want := []string{"powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcher, "-Model", "llama3", "-ExtraArguments", "--debug"}
	if !slices.Equal(argv, want) {
		t.Errorf("companionArgs = %v, want %v", argv, want)
	}

	argv = companionArgs(launcher, "", "", nil)
	want = []string{"powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcher}
	if !slices.Equal(argv, want) {
		t.Errorf("companionArgs = %v, want %v", argv, want)
	}
}