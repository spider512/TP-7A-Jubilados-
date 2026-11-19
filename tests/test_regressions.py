"""Pruebas aisladas: contratos del mock y fallas del padron sin tocar PostgreSQL."""
import importlib
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

municipal = importlib.import_module('app.main')
padron = importlib.import_module('mock.main')

NEIGHBOUR = {'sub':'neighbour-test','preferred_username':'vecino1',
             'realm_access':{'roles':['vecino']}}
AGENT = {'sub':'agent-test','preferred_username':'agente1',
         'realm_access':{'roles':['agente']}}


class MockContractTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(padron.app)

    def test_valid_conditions(self):
        for identifier,condition in [('JUB-001','jubilado'),('PEN-002','pensionado'),('NO-003','no_registrado')]:
            with self.subTest(identifier=identifier):
                r=self.client.post('/verificaciones',json={'identificador':identifier})
                self.assertEqual(r.status_code,200)
                self.assertEqual(r.json(),{'condicion':condition})

    def test_invalid_input_is_400_problem_details(self):
        for body in [{},{'identificador':''},{'identificador':' '},
                     {'identificador':3},{'identificador':'JUB-001','extra':True}]:
            with self.subTest(body=body):
                r=self.client.post('/verificaciones',json=body)
                self.assertEqual(r.status_code,400)
                self.assertEqual(r.headers['content-type'],'application/problem+json')
                self.assertEqual(r.json()['status'],400)

    def test_unavailable_is_503_problem_details(self):
        r=self.client.post('/verificaciones',json={'identificador':'ERR-004'})
        self.assertEqual(r.status_code,503)
        self.assertEqual(r.json()['status'],503)


class ExternalFailureTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        self.factory=sessionmaker(self.engine)
        municipal.Base.metadata.create_all(self.engine)
        def db():
            with self.factory() as session:
                yield session
        municipal.app.dependency_overrides[municipal.db_session]=db
        municipal.app.dependency_overrides[municipal.api_user]=lambda:AGENT
        self.client=TestClient(municipal.app,raise_server_exceptions=False)
        with self.factory() as session:
            self.item_id=municipal.create_application(session,NEIGHBOUR,'tarifa-social').id

    def tearDown(self):
        municipal.app.dependency_overrides.clear()
        self.engine.dispose()

    def decide(self):
        return self.client.patch('/api/v1/solicitudes/'+self.item_id,
                                 json={'estado':'aprobada','motivo':'Prueba aislada'})

    def assert_pending(self):
        with self.factory() as session:
            item=session.get(municipal.Application,self.item_id)
            self.assertEqual(item.estado,'pendiente')
            self.assertIsNone(item.agente_id)
            self.assertIsNone(item.resuelta_en)

    def test_malformed_external_response_is_503_and_keeps_pending(self):
        for content in [b'not-json',b'[]',b'null',b'{}',b'{"condicion":"desconocida"}',
                        b'{"condicion":[]}',b'{"condicion":{}}']:
            with self.subTest(content=content):
                with patch.object(municipal.httpx,'post',return_value=httpx.Response(200,content=content)):
                    r=self.decide()
                self.assertEqual(r.status_code,503)
                self.assertEqual(r.headers['content-type'],'application/problem+json')
                self.assert_pending()

    def test_timeout_is_503_and_keeps_pending(self):
        with patch.object(municipal.httpx,'post',side_effect=httpx.ReadTimeout('test')):
            self.assertEqual(self.decide().status_code,503)
        self.assert_pending()

    def test_negative_is_409_and_keeps_pending(self):
        with patch.object(municipal.httpx,'post',return_value=httpx.Response(200,json={'condicion':'no_registrado'})):
            self.assertEqual(self.decide().status_code,409)
        self.assert_pending()

    def test_positive_is_persisted_and_cannot_be_overwritten(self):
        with patch.object(municipal.httpx,'post',return_value=httpx.Response(200,json={'condicion':'jubilado'})):
            r=self.decide()
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.json()['estado'],'aprobada')
            self.assertEqual(self.decide().status_code,409)

    def test_callback_without_pending_login_is_400(self):
        r=self.client.get('/auth/callback',params={'state':'','code':'invalid'})
        self.assertEqual(r.status_code,400)


if __name__ == '__main__':
    unittest.main()
