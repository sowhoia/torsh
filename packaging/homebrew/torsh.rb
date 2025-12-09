class Torsh < Formula
  desc "Transmission TUI client"
  homepage "https://example.com/torsh"
  version "0.1.0"
  # TODO: replace with real PyPI sdist url and sha256 after publish
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

