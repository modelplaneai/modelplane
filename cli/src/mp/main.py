"""Modelplane CLI — deploy and manage AI model inference."""

import click

from mp import __version__
from mp.commands.delete import delete
from mp.commands.deploy import deploy
from mp.commands.deployments import deployments
from mp.commands.envs import envs
from mp.commands.init import init
from mp.commands.logs import logs
from mp.commands.models import models
from mp.commands.predict import predict
from mp.commands.status import status


@click.group()
@click.version_option(__version__, prog_name="mp")
def cli() -> None:
    """Modelplane CLI — deploy and manage AI model inference.

    Get started:

    \b
      mp init --team my-team          # set your team
      mp deploy <model>               # deploy from catalog
      mp deploy -f model.yaml         # deploy from YAML file
      mp status <name> --watch        # wait until ready
      mp predict <name> -i "prompt"   # test your model
    """


cli.add_command(init)
cli.add_command(models)
cli.add_command(deploy)
cli.add_command(status)
cli.add_command(predict)
cli.add_command(logs)
cli.add_command(deployments)
cli.add_command(delete)
cli.add_command(envs)
