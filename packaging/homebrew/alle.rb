# Homebrew formula for the headless `alle` channel (macOS + Linux).
#
# This is the canonical source of the formula. The `homebrew-tap` tap ships a
# copy of it; the release workflow updates the tap's copy only after the
# matching `alle-proxy` release exists on PyPI, using
# `scripts/update-homebrew-formula.py` to fill the `url`/`sha256` below with the
# digest PyPI recorded for the published sdist. The resource pins are taken from
# uv.lock (tests/test_homebrew_formula.py keeps them from drifting).
#
# Product boundary: this channel is deliberately headless. It installs the CLI,
# background daemon, loopback control API, and the version-locked bundled Web UI
# — never `rumps`, an `alle-tray` launcher, `alle.tray`, or `alle.companion`.
# The base wheel enforces that boundary for every native distribution channel.
class Alle < Formula
  include Language::Python::Virtualenv

  desc "Universal VPN client with rule-based routing (headless CLI + Web UI)"
  homepage "https://github.com/zydo/alle"
  url "https://files.pythonhosted.org/packages/96/fe/63196332a103c722b4a4260b94edc44ff43cdf20e1bd74055ca610e20498/alle_proxy-0.1.8.tar.gz"
  sha256 "caa34f0fd785dc8722abba22bfbe62912d8983af15268d7362c0fdfbdde20505"
  license "MIT"

  depends_on "libyaml"
  depends_on "python@3.13"

  # Runtime dependencies, pinned by checksum from uv.lock. Regenerate with
  # `brew update-python-resources` (or by hand from uv.lock) whenever the
  # locked versions change.
  resource "packaging" do
    url "https://files.pythonhosted.org/packages/d7/f1/e7a6dd94a8d4a5626c03e4e99c87f241ba9e350cd9e6d75123f992427270/packaging-26.2.tar.gz"
    sha256 "ff452ff5a3e828ce110190feff1178bb1f2ea2281fa2075aadb987c2fb221661"
  end

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end

  resource "pycountry" do
    url "https://files.pythonhosted.org/packages/de/1d/061b9e7a48b85cfd69f33c33d2ef784a531c359399ad764243399673c8f5/pycountry-26.2.16.tar.gz"
    sha256 "5b6027d453fcd6060112b951dd010f01f168b51b4bf8a1f1fc8c95c8d94a0801"
  end

  def install
    virtualenv_install_with_resources
  end

  # Native Homebrew supervision for the per-user daemon. On macOS this becomes a
  # LaunchAgent, on Linux a `systemd --user` unit — both run for the login
  # session and respawn on crash / self-restart-on-upgrade. Manage it with
  # `brew services`, not `alle daemon install` (see caveats).
  service do
    run [opt_bin/"alle", "applier"]
    environment_variables ALLE_SERVICE:        "1",
                          ALLE_SERVICE_OWNER:  "homebrew",
                          ALLE_SERVICE_PREFIX: opt_prefix.to_s,
                          PATH:                std_service_path_env
    keep_alive true
    log_path var/"log/alle.log"
    error_log_path var/"log/alle.log"
  end

  def caveats
    <<~EOS
      This is the headless alle channel: CLI, background daemon, loopback
      control API, and the bundled Web UI — no menu-bar app or tray.

      Manage the background daemon with brew services rather than
      `alle daemon install` (which would register a competing launchd/systemd
      unit for the same user):

        brew services start alle      # start now and at login
        brew services stop alle

      State lives in ~/.alle. Open the Web UI with `alle ui`.
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/alle version")

    site = Dir[libexec/"lib/python*/site-packages/alle"].first

    # The bundled Web UI is present and version-locked to the CLI package.
    assert_path_exists "#{site}/assets/index.html"

    # Product boundary: no GUI/tray/companion surface ships in this channel.
    refute_path_exists "#{site}/tray.py"
    refute_path_exists "#{site}/companion.py"
    refute_path_exists bin/"alle-tray"
    refute_path_exists libexec/"bin/alle-tray"
  end
end
