package launch

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// companionRepoURL points to the open-source companion integration that hosts
// the install and launch scripts used by the comet and chrome runners.
const companionRepoURL = "https://github.com/Lev0n82/AutonomousBrowserAutomation"

const companionInstallScriptURL = "https://raw.githubusercontent.com/Lev0n82/AutonomousBrowserAutomation/main/ollama-comet/Install-OllamaComet.ps1"

// companionDir returns the root directory of the installed companion integration.
func companionDir() (string, error) {
	switch runtime.GOOS {
	case "windows":
		local := os.Getenv("LOCALAPPDATA")
		if local == "" {
			return "", fmt.Errorf("companion integration not found: LOCALAPPDATA is not set")
		}
		return filepath.Join(local, "OllamaComet"), nil
	case "darwin", "linux":
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, ".ollama-comet"), nil
	default:
		return "", fmt.Errorf("companion integration is not supported on %s", runtime.GOOS)
	}
}

// companionLauncherName returns the launcher script name for the companion
// integration. browser is either "comet" or "chrome".
func companionLauncherName(browser string) string {
	switch runtime.GOOS {
	case "windows":
		if browser == "chrome" {
			return "Launch-AutonomousBrowser.ps1"
		}
		return "Launch-OllamaComet.ps1"
	default:
		if browser == "chrome" {
			return "launch-autonomous-browser.sh"
		}
		return "launch-ollama-comet.sh"
	}
}

// companionLauncher resolves the launcher executable for the companion integration.
func companionLauncher(browser string) (string, error) {
	root, err := companionDir()
	if err != nil {
		return "", err
	}
	launcher := filepath.Join(root, companionLauncherName(browser))
	if _, err := os.Stat(launcher); err != nil {
		return "", fmt.Errorf("companion integration not found at %s", launcher)
	}
	return launcher, nil
}

// companionArgs builds the launcher invocation: PowerShell script launchers
// get -File and named parameters, shell scripts get plain arguments.
func companionArgs(launcher string, browser, model string, extra []string) []string {
	if strings.EqualFold(filepath.Ext(launcher), ".ps1") {
		args := []string{"powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcher}
		if browser != "" {
			args = append(args, "-Browser", browser)
		}
		if model != "" {
			args = append(args, "-Model", model)
		}
		if len(extra) > 0 {
			args = append(args, "-ExtraArguments")
			args = append(args, extra...)
		}
		return args
	}

	args := []string{launcher}
	if browser != "" {
		args = append(args, "--browser", browser)
	}
	if model != "" {
		args = append(args, "--model", model)
	}
	return append(args, extra...)
}

// ensureCompanionInstalled returns the companion launcher, offering to install
// the companion integration when it is missing.
func ensureCompanionInstalled(browser string) (string, error) {
	if launcher, err := companionLauncher(browser); err == nil {
		return launcher, nil
	}

	if err := checkCompanionInstallerDependencies(); err != nil {
		return "", err
	}

	ok, err := ConfirmPrompt("The AutonomousBrowserAutomation companion integration is not installed. Install now?")
	if err != nil {
		return "", err
	}
	if !ok {
		return "", fmt.Errorf("companion integration installation cancelled")
	}

	bin, scriptArgs, err := companionInstallerCommand(runtime.GOOS)
	if err != nil {
		return "", err
	}

	fmt.Fprintf(os.Stderr, "\nInstalling AutonomousBrowserAutomation companion integration...\n")
	cmd := exec.Command(bin, scriptArgs...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("failed to install companion integration: %w", err)
	}

	launcher, err := companionLauncher(browser)
	if err != nil {
		return "", fmt.Errorf("companion integration was installed but the launcher was not found\n\nYou may need to restart your shell")
	}

	fmt.Fprintf(os.Stderr, "%sAutonomousBrowserAutomation companion integration installed successfully%s\n\n", ansiGreen, ansiReset)
	return launcher, nil
}

func checkCompanionInstallerDependencies() error {
	switch runtime.GOOS {
	case "windows":
		if _, err := exec.LookPath("powershell"); err != nil {
			return fmt.Errorf("the companion integration is not installed and required dependencies are missing\n\nInstall the following first:\n  PowerShell: https://learn.microsoft.com/powershell/\n\nThen re-run:\n  ollama launch comet")
		}
	default:
		return companionSupportedError()
	}
	return nil
}

func companionInstallerCommand(goos string) (string, []string, error) {
	switch goos {
	case "windows":
		return "powershell", []string{
			"-NoProfile",
			"-ExecutionPolicy",
			"Bypass",
			"-Command",
			"irm " + companionInstallScriptURL + " | iex",
		}, nil
	default:
		return "", nil, fmt.Errorf("unsupported platform for companion integration install: %s", goos)
	}
}

func companionSupportedError() error {
	return fmt.Errorf("the companion integration is currently supported on Windows only\n\nSee %s for details", companionRepoURL)
}

// Comet implements Runner for the Perplexity Comet agentic browser.
type Comet struct{}

func (c *Comet) String() string { return "Perplexity Comet" }

// Supported reports that the comet integration currently requires Windows.
func (c *Comet) Supported() error {
	if runtime.GOOS != "windows" {
		return companionSupportedError()
	}
	return nil
}

func (c *Comet) Run(model string, _ []LaunchModel, args []string) error {
	launcher, err := ensureCompanionInstalled("comet")
	if err != nil {
		return err
	}

	argv := companionArgs(launcher, "comet", model, args)
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func ensureCometInstalled() error {
	_, err := ensureCompanionInstalled("comet")
	return err
}