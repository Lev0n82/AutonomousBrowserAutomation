package launch

import (
	"os/exec"
	"runtime"
)

// Chrome implements Runner for driving a local Chrome or Chromium installation
// through the AutonomousBrowserAutomation companion integration.
type Chrome struct{}

func (c *Chrome) String() string { return "Chrome" }

// Supported reports that the chrome integration currently requires Windows.
func (c *Chrome) Supported() error {
	if runtime.GOOS != "windows" {
		return companionSupportedError()
	}
	return nil
}

func (c *Chrome) Run(model string, _ []LaunchModel, args []string) error {
	launcher, err := ensureCompanionInstalled("chrome")
	if err != nil {
		return err
	}

	argv := companionArgs(launcher, "chrome", model, args)
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func ensureChromeInstalled() error {
	_, err := ensureCompanionInstalled("chrome")
	return err
}