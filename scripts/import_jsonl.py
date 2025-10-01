import json
import os

import tqdm
import fire
from label_studio_sdk.client import LabelStudio


def read_jsonl(file_path: str):
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in tqdm.tqdm(f):
            yield json.loads(line)


def translate_to_labelstudio_format(data):
    return {
        'text': data.get('text_en', ''),
    }


def filter_empty_tasks(task: dict[str, str]) -> bool:
    return len(task.get('text', '').strip()) > 0


def upload_to_labelstudio(ls: LabelStudio, project_id: int, task: dict):
    ls.tasks.create(
        project=project_id,
        data=task
    )
    return None


def main(file_path: str, project_id: int, base_url: str = 'http://localhost:8080'):
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
                map(
                    translate_to_labelstudio_format,
                    read_jsonl(file_path))
            )
        )
    )


if __name__ == "__main__":
    fire.Fire(main)