"""The composition function's main CLI."""

import click
from crossplane.function import logging, runtime

from function import fn


@click.command()
@click.option("--debug", "-d", is_flag=True, help="Emit debug logs.")
@click.option(
    "--address",
    default="0.0.0.0:9443",
    show_default=True,
    help="Address at which to listen for gRPC connections",
)
@click.option("--tls-certs-dir", help="Serve using mTLS certificates.", envvar="TLS_SERVER_CERTS_DIR")
@click.option(
    "--insecure",
    is_flag=True,
    help="Run without mTLS credentials. If you supply this flag --tls-certs-dir will be ignored.",
)
def cli(debug: bool, address: str, tls_certs_dir: str, insecure: bool) -> None:  # noqa:FBT001
    """A Crossplane composition function."""
    try:
        level = logging.Level.INFO
        if debug:
            level = logging.Level.DEBUG
        logging.configure(level=level)
        runtime.serve(
            fn.FunctionRunner(),
            address,
            creds=runtime.load_credentials(tls_certs_dir),
            insecure=insecure,
        )
    except Exception as e:
        click.echo(f"Cannot run function: {e}")


if __name__ == "__main__":
    cli()
