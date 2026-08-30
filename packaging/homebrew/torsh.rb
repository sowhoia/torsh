# Tap-style formula: installs torsh (and its PyPI dependencies) into a
# private virtualenv under libexec. Pip resolves dependencies at install
# time, which keeps the formula maintenance-free for a personal tap
# (homebrew-core would require vendored `resource` blocks instead).
class Torsh < Formula
  desc "Self-contained BitTorrent client for the terminal (engine included)"
  homepage "https://github.com/sowhoia/torsh"
  version "1.0.0"
  # After publishing to PyPI, set sha256 to the sdist hash:
  #   curl -sL https://pypi.org/pypi/torsh/json | jq -r '.urls[] | select(.packagetype=="sdist") | .digests.sha256'
  url "https://files.pythonhosted.org/packages/source/t/torsh/torsh-#{version}.tar.gz"
  sha256 "40001319eee2d58e0ba9b0e7d1eef51b9d15b6a59b2cae68eeb6bae7832cd482"
  license "MIT"

  depends_on "python@3.12"

  def install
    python = Formula["python@3.12"].opt_bin/"python3.12"
    system python, "-m", "venv", libexec
    system libexec/"bin/pip", "install", "--no-cache-dir", buildpath
    bin.install_symlink libexec/"bin/torsh"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/torsh --version")
  end
end
