# pypi
A package index for GitHub.

## What is this?

This repository is a combination of two parts:

1. It is a PyPi repository for the Center for an Informed Public.
2. It is a project that can be used to generate your PyPi repository using GitHub pages.

How is this different from other static pypi generators? This one takes a list of GitHub repositories, uses the GitHub
API to get a list of releases for those repositories, and then makes static pages that deploy well with GitHub Pages.

## Generating static files

First, create a file that will contain the list of all of the repositories that want to poll for new releases. It might
look like this:

    uwcip/ciptools

In the above example we have exactly one repository called `ciptools` and it is under the `uwcip` owner.

Then you will invoke the script:

    $ echo $GITHUB_TOKEN | ghpypi --output docs --repositories repositories --title "CIP PyPi" --token-stdin

The newly built static index can now be found under `docs` and you can use GitHub Pages to share the `docs` directory.

## Automatically generating static files

You might want to put this into some sort of cron job to rebuild on a regular basis. We can use GitHub Actions to
accomplish that. Create an actions workflow file that looks like this:

```yaml
name: Generate Index

on:
  workflow_dispatch:
  repository_dispatch:
  schedule:
    - cron: "0 * * * *"

jobs:
  generate:
    runs-on: ubuntu-latest

    steps:
      - name: checkout
        uses: actions/checkout@v2

      - name: setup python
        uses: actions/setup-python@v2
        with:
          python-version: 3.9

      - name: generate index
        run: |
          pip install poetry
          poetry install --no-ansi --no-interaction --no-dev
          poetry run ghpypi --output=docs --repositories=repositories.txt --title="My Private PyPi" --token=${{ secrets.GITHUB_TOKEN }}

      - uses: stefanzweifel/git-auto-commit-action@v4
        with:
          commit_message: automatic index update [skip ci]
```

If you want to trigger a rebuild of the index when another repository does something then you can do that with an action
as well. Add this to your other repository to trigger a build in this repository:

```yaml
  - name: update pypi
    uses: octokit/request-action@v2.x
    with:
      route: POST /repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches
      owner: myorg
      repo: mypypi
      workflow: generate-index.yaml
      ref: main
    env:
      GITHUB_TOKEN: ${{ secrets.WORKFLOW_TOKEN }}
```

The `WORKFLOW_TOKEN` is a personal access token that is granted admin rights to repositories in your organization.
You cannot use the regular `GITHUB_TOKEN` secret because GitHub does not want you to inadvertently create circular
actions. (You can purposely create circular actions, though!)

## Using your deployed index server with pip (or poetry)

When running pip, pass `--extra-index-url https://myorg.github.io/mypypi/simple` or set the environment variable
`PIP_EXTRA_INDEX_URL==https://myorg.github.io/mypypi/simple`. If you're using [poetry](https://python-poetry.org/)
then simply add this to your `pyproject.toml` file:

```toml
[[tool.poetry.source]]
name = "mypypi"
url = "https://myorg.github.io/mypypi/simple/"
```

## TODO

This project needs some tests.

## Credits

This package is based heavily on [dumb-pypi](https://github.com/chriskuehl/dumb-pypi) which was created by and is
maintained by [Chris Kuehl](https://github.com/chriskuehl).
