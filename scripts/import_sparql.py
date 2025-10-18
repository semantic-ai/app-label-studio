import re
import uuid
from abc import ABC, abstractmethod
from string import Template
import os
from datetime import datetime

from label_studio_sdk.label_interface import LabelInterface
from tqdm import tqdm
from typing import Iterator, Any, Optional

from lxml import etree

import fire
from label_studio_sdk.client import LabelStudio
from label_studio_sdk.label_interface.objects import PredictionValue

from SPARQLWrapper import SPARQLWrapper, JSON, POST

activity_id = uuid.uuid4()

def sparql_escape_uri(obj):
    """Converts the given URI to a SPARQL-safe RDF object string with the right RDF-datatype. """
    obj = str(obj)
    return '<' + re.sub(r'[\\"<>]', lambda s: "\\" + s.group(0), obj) + '>'


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


def filter_empty_tasks(task: dict[str, str] | None) -> bool:
    return task is not None and len(task.get('task', {}).get('data', {}).get('text', '').strip()) > 0


class LabelStudioExt(LabelStudio):
    def find_userid_by_username(self, username: str) -> int | None:
        username = username.split('/')[-1]
        users = self.users.list()
        for user in users:
            if user.username == username:
                return user.id
        return None


class Annotation(ABC):
    @classmethod
    def create_annotation(cls, uri: str, user: str, annotation: Any) -> Optional['Annotation']:
        if annotation['type'] == 'labels':
            return NERAnnotation(
                uri,
                annotation['value']['labels'][0],
                annotation['value']['start'],
                annotation['value']['end'],
                user,
                "http://www.w3.org/ns/prov#Person"
            )

        if annotation['type'] == 'choices':
            return LinkingAnnotation(
                uri,
                annotation['value']['choices'],
                user,
                "http://www.w3.org/ns/prov#Person"
            )

        return None

    @abstractmethod
    def to_labelstudio_result(self) -> dict:
        pass

    @abstractmethod
    def add_to_triplestore(self, sparql_query: SPARQLWrapper):
        pass

    @classmethod
    @abstractmethod
    def retrieve_for_uri(cls, uri: str, sparql_query: SPARQLWrapper) -> Iterator['NERAnnotation']:
        pass


class LinkingAnnotation(Annotation):
    def __init__(self, uri: str, class_uri: str, agent: str, agent_type: str):
        self.uri = uri
        self.class_uri = class_uri
        self.agent = agent
        self.agent_type = agent_type

    @classmethod
    def retrieve_for_uri(cls, uri: str, sparql_query: SPARQLWrapper) -> Iterator['NERAnnotation']:
        query_template = Template("""
        PREFIX oa:  <http://www.w3.org/ns/oa#>
        PREFIX prov:  <http://www.w3.org/ns/prov#>

        SELECT ?body ?agent ?agentType
        WHERE {
          ?annotation a oa:Annotation ;
                       oa:hasTarget ?target .
          ?annotation oa:hasBody ?body.
          OPTIONAL { ?annotation oa:motivation ?motivation . }

          # Example filter (uncomment and edit as needed):
          FILTER(?target = $uri)
          FILTER(?motivation = "linking")

          OPTIONAL {
              ?activity a prov:Activity ;
              prov:generated ?annotation ;
              prov:wasAssociatedWith ?agent .
              
              OPTIONAL { ?agent rdf:type ?agentType . }
          }
        }
        """)
        query = query_template.substitute(
            uri=sparql_escape_uri(uri)
        )
        sparql_query.setQuery(query)
        query_result = sparql_query.query().convert()
        if not query_result['results']['bindings']:
            return
            yield

        for item in query_result['results']['bindings']:
            yield cls(uri, item['body']['value'], item['agent']['value'], item['agentType']['value'])

    def to_labelstudio_result(self):
        return {
            "type": "choices",
            "value": {"choices": [self.class_uri]},
            "origin": "manual", "to_name": "text", "from_name": "entities"
        }

    def add_to_triplestore(self, sparql_query: SPARQLWrapper):
        query_template = Template("""
            PREFIX ex:  <http://example.org/>
            PREFIX oa:  <http://www.w3.org/ns/oa#>
            PREFIX mu:  <http://mu.semte.ch/vocabularies/core/>
            PREFIX prov:  <http://www.w3.org/ns/prov#>
            PREFIX foaf:  <http://xmlns.com/foaf/0.1/>
            PREFIX dct:  <http://purl.org/dc/terms/>
            PREFIX skolem:  <http://www.example.org/id/.well-known/genid/>
            PREFIX nif:  <http://persistence.uni-leipzig.org/nlp2rdf/ontologies/nif-core#>

            INSERT {
              $activity_id a prov:Activity;
                 prov:generated $annotation_id;
                 prov:wasAssociatedWith ex:$user .

              $annotation_id a oa:Annotation ;
                             mu:uuid "$id";
                             oa:hasBody $clz ;
                             nif:confidence 1 ;
                             oa:motivation "linking" ;
                             oa:hasTarget $uri .
            } WHERE {
              FILTER NOT EXISTS { 
                ?existingAnn a oa:Annotation ;
                    oa:hasBody $clz ;
                    oa:motivation "linking" ;
                    oa:hasTarget $uri .
                    
                ?existingAct a prov:Activity ;
                 prov:generated ?existingAnn ;
                 prov:wasAssociatedWith ex:$user .
              }
            }
            """)
        query_string = query_template.substitute(
            id=str(uuid.uuid1()),
            annotation_id=sparql_escape_uri("http://example.org/{0}".format(uuid.uuid4())),
            activity_id=sparql_escape_uri("http://example.org/{0}".format(activity_id)),
            uri=sparql_escape_uri(self.uri),
            user=self.agent,
            clz=" , ".join(map(sparql_escape_uri, self.class_uri))
        )

        sparql_query.setQuery(query_string)
        try:
            sparql_query.query()
            print("Data inserted successfully.")
        except Exception as e:
            print("Error:", e)
            raise


class NERAnnotation(Annotation):
    def __init__(self, uri: str, class_uri: str, start: int, end: int, agent: str, agent_type: str):
        self.uri = uri
        self.class_uri = class_uri
        self.start = start
        self.end = end
        self.agent = agent
        self.agent_type = agent_type

    @classmethod
    def retrieve_for_uri(cls, uri: str, sparql_query: SPARQLWrapper) -> Iterator['NERAnnotation']:
        query_template = Template("""
        PREFIX oa:  <http://www.w3.org/ns/oa#>
    
        SELECT ?body ?start ?end ?agent ?agentType
        WHERE {
          ?annotation a oa:Annotation ;
                       oa:hasTarget ?target .
          ?target a oa:SpecificResource ;
                  oa:source ?source; oa:selector ?selector .
          ?selector a oa:TextPositionSelector ;
                  oa:start ?start; oa:end ?end .
          ?annotation oa:hasBody ?body.
          OPTIONAL { ?annotation oa:motivation ?motivation . }
        
          # Example filter (uncomment and edit as needed):
          FILTER(?source = $uri)
          FILTER(?motivation = "classifying")
          
          OPTIONAL {
              ?activity a prov:Activity ;
              prov:generated ?annotation ;
              prov:wasAssociatedWith ?agent .
              
              OPTIONAL { ?agent rdf:type ?agentType . }
          }
        }
        """)
        query = query_template.substitute(
            uri=sparql_escape_uri(uri)
        )
        sparql_query.setQuery(query)
        query_result = sparql_query.query().convert()
        for item in query_result['results']['bindings']:
            yield cls(uri, item['body']['value'], item['start']['value'], item['end']['value'], item['agent']['value'], item['agentType']['value'])

    def to_labelstudio_result(self):
        return {
            "type": "labels",
            "value": {"end": self.end, "start": self.start, "labels": [self.class_uri]},
            "origin": "manual", "to_name": "text", "from_name": "label"
        }

    def to_labelstudio_prediction(self, li: LabelInterface) -> PredictionValue:
        return li.get_control('entities').label(choices=self.class_uri)

    def add_to_triplestore(self, sparql_query: SPARQLWrapper):
        query_template = Template("""
            PREFIX ex:  <http://example.org/>
            PREFIX oa:  <http://www.w3.org/ns/oa#>
            PREFIX mu:  <http://mu.semte.ch/vocabularies/core/>
            PREFIX prov:  <http://www.w3.org/ns/prov#>
            PREFIX foaf:  <http://xmlns.com/foaf/0.1/>
            PREFIX dct:  <http://purl.org/dc/terms/>
            PREFIX skolem:  <http://www.example.org/id/.well-known/genid/>
            PREFIX nif:  <http://persistence.uni-leipzig.org/nlp2rdf/ontologies/nif-core#>

            INSERT {
              $activity_id a prov:Activity;
                 prov:generated $annotation_id;
                 prov:wasAssociatedWith ex:$user .

              $annotation_id a oa:Annotation ;
                             mu:uuid "$id";
                             oa:hasBody $clz ;
                             nif:confidence 1 ;
                             oa:motivation "classifying" ;
                             oa:hasTarget $part_of_id .

              $part_of_id a oa:SpecificResource ;
                          oa:source $uri ;
                          oa:selector $selector_id .

              $selector_id a oa:TextPositionSelector ;
                           oa:start $start ;
                           oa:end $end .
            } WHERE {
              FILTER NOT EXISTS {
                ?existingAnn a oa:Annotation ;
                    oa:hasBody $clz ;
                    oa:motivation "classifying" ;
                    oa:hasTarget ?existingTarget .
                    
                ?existingAct a prov:Activity ;
                 prov:generated ?existingAnn ;
                 prov:wasAssociatedWith ex:$user .

                ?existingTarget a oa:SpecificResource ;
                    oa:source $uri ;
                    oa:selector ?existingSelector .

                ?existingSelector a oa:TextPositionSelector ;
                      oa:start $start ;
                      oa:end $end .
              }
            }
            """)
        query_string = query_template.substitute(
            id=str(uuid.uuid1()),
            annotation_id=sparql_escape_uri("http://example.org/{0}".format(uuid.uuid4())),
            activity_id=sparql_escape_uri("http://example.org/{0}".format(activity_id)),
            selector_id=sparql_escape_uri("http://www.example.org/id/.well-known/genid/{0}".format(uuid.uuid4())),
            part_of_id=sparql_escape_uri("http://www.example.org/id/.well-known/genid/{0}".format(uuid.uuid4())),
            uri=sparql_escape_uri(self.uri),
            start=self.start,
            end=self.end,
            user=self.agent,
            clz=sparql_escape_uri(self.class_uri)
        )

        sparql_query.setQuery(query_string)
        try:
            sparql_query.query()
            print("Data inserted successfully.")
        except Exception as e:
            print("Error:", e)
            raise


class AnnotationList(list[Annotation]):
    def get_by_agent(self, user: str) -> 'AnnotationList':
        return AnnotationList([
            annotation
            for annotation in self
            if annotation.agent == user
        ])

    def get_users(self) -> list[str]:
        return list({ annotation.agent for annotation in self if annotation.agent_type == "http://www.w3.org/ns/prov#Person" })

    def get_models(self) -> list[str]:
        return list({ annotation.agent for annotation in self if annotation.agent_type == "https://data.vlaanderen.be/ns/lblod#AIComponent" })

    def add_to_labelstudio(self, ls: LabelStudioExt, task_id: int):
        for user in self.get_users():
            result = [
                annotation.to_labelstudio_result()
                for annotation in self.get_by_agent(user)
            ]

            ls.annotations.create(
                task_id,
                result=result,
                completed_by=ls.find_userid_by_username(user)
            )

        for model in self.get_models():
            result = [
                annotation.to_labelstudio_result()
                for annotation in self.get_by_agent(model)
            ]

            prediction = PredictionValue(
                model_version=model,
                score=1,
                result=result
            )

            ls.predictions.create(task=task_id, **prediction.model_dump())



class SyncTripleStoreAndLabelStudio:
    def __init__(self, sparql_endpoint: str, sparql_graph: str, labelstudio_endpoint: str, labelstudio_api_key: str):
        super().__init__()
        self.ls = LabelStudioExt(base_url=labelstudio_endpoint, api_key=labelstudio_api_key)
        self.sparql_endpoint = sparql_endpoint
        self.sparql_graph = sparql_graph
        self.activity_id = uuid.uuid4()

    def _create_wrapper(self, write: bool = False) -> SPARQLWrapper:
        sparql = SPARQLWrapper(self.sparql_endpoint, defaultGraph=self.sparql_graph, returnFormat=JSON)
        if write:
            sparql.setMethod(POST)
            sparql.setRequestMethod("urlencoded")
            sparql.queryType = "UPDATE"
        return sparql

    def _query(self, query: str, write: bool = False) -> SPARQLWrapper:
        sparql = self._create_wrapper(write)
        sparql.setQuery(query)
        return sparql

    def generate_label_config(self) -> str:
        root = etree.Element("View")
        root.append(etree.Element("Text", name="text", value='$text'))
        labels = etree.Element("Labels", name="label", toName='text')
        choices = etree.Element("Choices", name='entities', toName='text', choice='multiple', showInLine='true')
        query = """
        PREFIX oa:  <http://www.w3.org/ns/oa#>

        SELECT distinct ?motivation ?body
        WHERE {{
          ?annotation a oa:Annotation .
          ?annotation oa:hasBody ?body .
          ?annotation oa:motivation ?motivation .
        }}
        """
        query_result = self._query(query).query().convert()
        for item in query_result['results']['bindings']:
            if item['motivation']['value'] == 'classifying':
                labels.append(etree.Element("Label", value=item['body']['value']))

            if item['motivation']['value'] == 'linking':
                choices.append(etree.Element("Choice", value=item['body']['value']))

        if len(labels):
            root.append(labels)

        if len(choices):
            extra_label = etree.Element("View", style='"box-shadow: 2px 2px 5px #999; padding: 20px; margin-top: 2em; border-radius: 5px;"')
            extra_label.append(etree.Element("Header", value='Choose'))
            extra_label.append(choices)
            root.append(extra_label)

        return etree.tostring(root).decode()

    def create_project(self) -> int:
        label_config = self.generate_label_config()
        project = self.ls.projects.create(
            title="Automated export {0}".format(datetime.now().isoformat()),
            label_config=label_config
        )
        return project.id

    def read_decisions(self):
        query = """
        select distinct ?s ?decision_basis AS ?content where {{
            OPTIONAL {{ ?s <http://data.europa.eu/eli/eli-dl#decision_basis> ?decision_basis }}
        }}
        """
        query_result = self._query(query).query().convert()
        for item in tqdm(query_result['results']['bindings']):
            yield wrap_to_labelstudio_format(
                item["s"]["value"],
                item["content"]["value"]
            )

    def migrate_to_labelstudio_tasks(self, project_id: int, task: dict):
        t = self.ls.tasks.create(
            project=project_id,
            **task
        )
        annotations = AnnotationList()
        annotations.extend(NERAnnotation.retrieve_for_uri(task['meta']['uri'], self._create_wrapper(False)))
        annotations.extend(LinkingAnnotation.retrieve_for_uri(task['meta']['uri'], self._create_wrapper(False)))
        annotations.add_to_labelstudio(self.ls, t.id)


    def migrate_to_labelstudio_project(self, project_id: int):
        list(
            map(
                lambda x: self.migrate_to_labelstudio_tasks(
                    project_id,
                    x['task']
                ),
                filter(
                    filter_empty_tasks,
                    self.read_decisions()
                )
            )
        )

    def sync_triplestore_to_labelstudio(self):
        project_id = self.create_project()
        self.migrate_to_labelstudio_project(project_id)

    def sync_labelstudio_to_triplestore(self, project_id: int):
        for l in self.ls.tasks.list(project=project_id):
            if l.annotations:
                decision_uri = l.meta.get('uri')
                if decision_uri:
                    for annotator, annotation in zip(l.annotators, l.annotations):
                        for res in annotation['result']:
                            annotation = Annotation.create_annotation(decision_uri, self.ls.users.get(annotator).username, res)
                            annotation.add_to_triplestore(self._create_wrapper(True))


def import_to_labelstudio(
        sparql_endpoint: str = 'http://localhost:8890/sparql',
        base_url: str = 'http://localhost:8080',
        sparql_graph: str = 'http://mu.semte.ch/graphs/oslo-temp'
):
    api_key = os.getenv('LABELSTUDIO_TOKEN')
    synchronizer = SyncTripleStoreAndLabelStudio(sparql_endpoint, sparql_graph, base_url, api_key)
    synchronizer.sync_triplestore_to_labelstudio()


def export_from_labelstudio(
        project_id: int,
        sparql_endpoint: str = 'http://localhost:8890/sparql',
        base_url: str = 'http://localhost:8080',
        sparql_graph: str = 'http://mu.semte.ch/graphs/oslo-temp'
):
    api_key = os.getenv('LABELSTUDIO_TOKEN')
    synchronizer = SyncTripleStoreAndLabelStudio(sparql_endpoint, sparql_graph, base_url, api_key)
    synchronizer.sync_labelstudio_to_triplestore(project_id)

if __name__ == "__main__":
    fire.Fire({
        'import': import_to_labelstudio,
        'export': export_from_labelstudio
    })