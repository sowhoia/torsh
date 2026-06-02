class Torsh < Formula
  desc "Modern Transmission TUI client for the terminal"
  homepage "https://github.com/sowhoia/torsh"
  version "0.2.0"
  # After publishing to PyPI, set sha256 to the sdist hash:
  #   curl -sL https://pypi.org/pypi/torsh/#{version}/json | jq -r '.urls[] | select(.packagetype=="sdist") | .digests.sha256'
  url "https://files.pythonhosted.org/packages/source/t/torsh/torsh-#{version}.tar.gz"
  sha256 "<fill-me>"

  depends_on "python@3.11"
  depends_on "pipx" => :build

  def install
    system "pipx", "install", "--spec", buildpath.to_s, "torsh"
    bin.install_symlink Dir.home/".local/pipx/venvs/torsh/bin/torsh"
  end

  test do
    system "#{bin}/torsh", "--version"
  end
end

