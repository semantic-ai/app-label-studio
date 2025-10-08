import json
import os
from typing import Iterator

import tqdm
import fire
from label_studio_sdk.client import LabelStudio
from SPARQLWrapper import SPARQLWrapper, JSON


def wrap_to_labelstudio_format(uri: str, text: str) -> dict[str, dict[str, dict[str, str]]]:
    return {
        'task': {
            'meta': {
                'uri': uri
            },
            'data': {
                'text': text
            }
        }
    }


def read_jsonl(file_path: str):
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in tqdm.tqdm(f):
            data = json.loads(line)
            yield wrap_to_labelstudio_format(
                data.get('subject', ''),
                data.get('attributes', {}).get('prov:value', [''])[0]
            )


def read_sparql(query: str, sparql_endpoint: str):
    sparqlQuery = SPARQLWrapper(sparql_endpoint, returnFormat=JSON)
    sparqlQuery.setQuery(query)
    query_result = sparqlQuery.query().convert()
    for item in query_result['results']['bindings']:
        yield wrap_to_labelstudio_format(
            item["s"]["value"],
            item["decision_basis"]["value"]
        )


def filter_empty_tasks(task: dict[str, str] | None) -> bool:
    return task is not None and len(task.get('task', {}).get('data', {}).get('text', '').strip()) > 0


def upload_to_labelstudio(ls: LabelStudio, project_id: int, task: dict):
    ls.tasks.create(
        project=project_id,
        **task['task']
    )
    return None


def get_reader(file_path: str | None, sparql_endpoint: str | None) -> Iterator[dict[str, dict[str, dict[str, str]]]]:
    if file_path is not None:
        yield from read_jsonl(file_path)

    if sparql_endpoint is not None:
        query = """
        select distinct ?s ?decision_basis where {{
            OPTIONAL {{ ?s <http://data.europa.eu/eli/eli-dl#decision_basis> ?decision_basis }}
        }}
        """
        yield from read_sparql(query, sparql_endpoint)


def main(
        project_id: int,
        file_path: str | None = None,
        sparql_endpoint: str | None = None,
        base_url: str = 'http://localhost:8080'
):
    api_key = os.getenv('LABELSTUDIO_TOKEN')
    ls = LabelStudio(base_url=base_url, api_key=api_key)
    _ = list(
        map(
            lambda x: upload_to_labelstudio(
                ls,
                project_id,
                x
            ),
            filter(
                filter_empty_tasks,
                get_reader(file_path, sparql_endpoint)
            )
        )
    )


if __name__ == "__main__":
    fire.Fire(main)