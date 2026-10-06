"""corewatch: a Core Temp-style hardware monitor for Linux."""


def main() -> int:
    from corewatch.cli import main as cli_main

    return cli_main()
