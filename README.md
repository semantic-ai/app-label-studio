# Labelstudio

This repository contains a slightly modified docker compose stack to get [labelstudio](https://github.com/HumanSignal/label-studio) up and running,
along with some example scripts 

## Starting labelstudio

```commandline
docker compose up
```

visit http://localhost:8080 and signup. After signup you will be automatically logged in.

Click on your account top-right, and click account & settings. Then chooce Personal Access Token. Create a new token and copy.

```commandline
export LABELSTUDIO_TOKEN='<token>'
```

## Importing data
This repository contains some scripts to import data from and to labelstudio. As data formats vary, these server mainly
as examples to be modified.

First, head into labelstudio and create an NLP type project. For instance, text classification. Next, note the ID of the project
The ID can be found in the URL, e.g. for `http://localhost:8080/projects/2/data` the ID is 2.

Then, install dependencies:
```commandline
uv sync
```
To import for instance, a JSONL datafile from Freiburg
```commandline
python scripts/import_jsonl.py --file-path=freiburg_decisions_raw.jsonl --project-id=2
```
Additionally, the script also supports reading data from a SparQL endpoint (though you may need to edit the query in the script)
Example of this:
```commandline
python scripts/import_jsonl.py --sparql-endpoint='http://localhost:8890/sparql' --project-id=2
```