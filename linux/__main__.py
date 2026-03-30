"""Entry point: python3 -m linux"""

from .daemon import Daemon


def main():
    daemon = Daemon()
    daemon.run()


if __name__ == "__main__":
    main()
