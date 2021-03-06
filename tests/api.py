
import json

import pytest

from django.core import mail

from rest_framework import status

from index_service.crypto import decode_key
from index_service.models import Entry, Identity
from index_service.logic import UpdateRequest
from index_service.utils import AccountingAuthorization


class RootTest:
    def test_root(self, api_client):
        response = api_client.get('/api/v0/')
        assert len(response.data) == 3


class KeyTest:
    path = '/api/v0/key/'

    def test_get_key(self, api_client):
        response = api_client.get(self.path)
        assert response.status_code == status.HTTP_200_OK
        decode_key(response.data['public_key'])
        # The public key is ephemeral (generated when the server starts); can't really check much else.


class SearchTest:
    path = '/api/v0/search/'

    @pytest.fixture(params=('get', 'post'))
    def search_client(self, request, api_client):
        def client(query):
            if request.param == 'get':
                return api_client.get(self.path, query)
            else:
                transformed_query = []
                for field, value in query.items():
                    if isinstance(value, (list, tuple)):
                        for v in value:
                            transformed_query.append({'field': field, 'value': v})
                    else:
                        transformed_query.append({'field': field, 'value': value})
                q = json.dumps({'query': transformed_query})
                return api_client.post(self.path, q, content_type='application/json')
        return client

    def test_get_identity(self, search_client, email_entry):
        response = search_client({'email': email_entry.value})
        assert response.status_code == status.HTTP_200_OK, response.json()
        identities = response.data['identities']
        assert len(identities) == 1
        identity = identities[0]
        assert identity['alias'] == 'qabel_user'
        assert identity['drop_url'] == 'http://127.0.0.1:6000/qabel_user'
        matches = identity['matches']
        assert len(matches) == 1
        assert {'field': 'email', 'value': email_entry.value} in matches

    def test_get_no_identity(self, search_client):
        response = search_client({'email': 'no_such_email@example.com'})
        assert response.status_code == status.HTTP_200_OK, response.json()
        assert len(response.data['identities']) == 0

    def test_multiple_fields_are_ORed(self, search_client, email_entry):
        response = search_client({'email': email_entry.value, 'phone': '123456789'})
        assert response.status_code == status.HTTP_200_OK, response.json()
        identities = response.data['identities']
        assert len(identities) == 1
        identity = identities[0]
        assert identity['alias'] == 'qabel_user'
        assert identity['drop_url'] == 'http://127.0.0.1:6000/qabel_user'
        matches = identity['matches']
        assert len(matches) == 1
        assert {'field': 'email', 'value': email_entry.value} in matches

    def test_match_is_exact(self, search_client, email_entry):
        response = search_client({'email': email_entry.value + "a"})
        assert response.status_code == status.HTTP_200_OK, response.json()
        assert not response.data['identities']
        response = search_client({'email': "a" + email_entry.value})
        assert response.status_code == status.HTTP_200_OK, response.json()
        assert not response.data['identities']

    def test_cross_identity(self, search_client, email_entry, identity):
        identity2 = Identity(alias='1234', drop_url='http://127.0.0.1:6000/qabel_1234', public_key=identity.public_key)
        identity2.save()
        phone1, phone2 = '+491234', '+491235'
        email = 'bar@example.net'
        Entry(identity=identity2, field='phone', value=phone1).save()
        Entry(identity=identity2, field='phone', value=phone2).save()
        Entry(identity=identity2, field='email', value=email).save()

        response = search_client({
            'email': (email_entry.value, email),
            'phone': phone1,
        })
        assert response.status_code == status.HTTP_200_OK, response.json()
        identities = response.data['identities']
        assert len(identities) == 2

        expected1 = {
            'alias': '1234',
            'drop_url': 'http://127.0.0.1:6000/qabel_1234',
            'public_key': identity.public_key,
            'matches': [
                {'field': 'email', 'value': email},
                {'field': 'phone', 'value': phone1},
            ]
        }
        assert expected1 in identities

    def test_unknown_field(self, search_client):
        response = search_client({'no such field': '...'})
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()

    def test_missing_query(self, api_client):
        response = api_client.post(self.path, '{}', content_type='application/json')
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()

    def test_empty_query(self, search_client):
        response = search_client({})
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
        # "No or unknown field spec'd" or "No fields spec'd"
        assert 'fields specified' in response.json()['error']


class UpdateTest:
    path = '/api/v0/update/'

    def _update_request_with_no_verification(self, api_client, mocker, simple_identity, items, **kwargs):
        request = json.dumps({
            'identity': simple_identity,
            'items': items
        })
        # Short-cut verification to execution
        mocker.patch.object(UpdateRequest, 'start_verification', lambda self, *_: self.execute())
        response = api_client.put(self.path, request, content_type='application/json', **kwargs)
        assert response.status_code == status.HTTP_204_NO_CONTENT

    def _search(self, api_client, what):
        response = api_client.get(SearchTest.path, what)
        assert response.status_code == status.HTTP_200_OK, response.json()
        result = response.data['identities']
        assert len(result) == 1
        assert result[0]['alias'] == 'public alias'
        assert result[0]['drop_url'] == 'http://example.com'

    def test_create(self, api_client, mocker, simple_identity):
        email = 'onlypeople_who_knew_this_address_already_can_find_the_entry@example.com'
        self._update_request_with_no_verification(api_client, mocker, simple_identity, [{
            'action': 'create',
            'field': 'email',
            'value': email,
        }])
        self._search(api_client, {'email': email})

    @pytest.mark.parametrize('accept_language', (
        'de-de',  # an enabled language, also the default
        'ko-kr',  # best korea
        None,  # no header set
    ))
    @pytest.mark.parametrize('phone_number, search_number', (
        ('+661234', '+661234'),
        ('1234', '+491234'),
    ))
    def test_create_phone_normalization(self, api_client, mocker, simple_identity, phone_number, accept_language, search_number):
        self._test_create_phone(api_client, mocker, simple_identity, phone_number, accept_language, search_number)

    @pytest.mark.parametrize('phone_number, accept_language, search_number', (
        ('555', 'en-us', '+1555'),
    ))
    def test_create_phone(self, api_client, mocker, simple_identity, phone_number, accept_language, search_number):
        self._test_create_phone(api_client, mocker, simple_identity, phone_number, accept_language, search_number)

    def _test_create_phone(self, api_client, mocker, simple_identity, phone_number, accept_language, search_number):
        kwargs = {}
        if accept_language:
            kwargs['HTTP_ACCEPT_LANGUAGE'] = accept_language
        self._update_request_with_no_verification(api_client, mocker, simple_identity, [{
            'action': 'create',
            'field': 'phone',
            'value': phone_number,
        }], **kwargs)
        self._search(api_client, {'phone': search_number})

    @pytest.fixture
    def delete_prerequisite(self, api_client, email_entry):
        # Maybe use pytest-bdd here?
        # pls more fixtures
        request = json.dumps({
            'identity': {
                'public_key': email_entry.identity.public_key,
                'drop_url': email_entry.identity.drop_url,
                'alias': email_entry.identity.alias,
            },
            'items': [
                {
                    'action': 'delete',
                    'field': 'email',
                    'value': email_entry.value,
                }
            ]
        })
        response = api_client.put(self.path, request, content_type='application/json')
        assert response.status_code == status.HTTP_202_ACCEPTED

        assert len(mail.outbox) == 1
        message = mail.outbox.pop()
        assert message.to == [email_entry.value]
        message_context = message.context
        assert message_context['identity'] == email_entry.identity

        return message_context

    def test_delete_confirm(self, api_client, delete_prerequisite, email_entry):
        confirm_url = delete_prerequisite['confirm_url']

        # At this point the entry still exists
        assert Entry.objects.filter(value=email_entry.value).count() == 1
        # User clicks the confirm link
        response = api_client.get(confirm_url)
        assert response.status_code == status.HTTP_200_OK
        # Entry should be gone now
        assert Entry.objects.filter(value=email_entry.value).count() == 0

    def test_delete_deny(self, api_client, delete_prerequisite, email_entry):
        deny_url = delete_prerequisite['deny_url']

        assert Entry.objects.filter(value=email_entry.value).count() == 1
        # User clicks the deny link
        response = api_client.get(deny_url)
        assert response.status_code == status.HTTP_200_OK
        # Entry should still exist
        assert Entry.objects.filter(value=email_entry.value).count() == 1

    @pytest.mark.parametrize('invalid_request', [
        {},
        {'items': "a string?"},
        {'items': []},
        {
            'items': [
                {
                    'action': 'well that ain’t valid'
                }
            ]
        },
    ])
    def test_invalid(self, api_client, invalid_request, simple_identity):
        invalid_request['identity'] = simple_identity
        request = json.dumps(invalid_request)
        response = api_client.put(self.path, request, content_type='application/json')
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_encrypted(self, api_client, settings):
        encrypted_json = bytes.fromhex('cc0330af7d17d21a58f3c277897b12904059606a323807c3a52d07c50b1814114a1472efb3f3ff9'
                                       '73fbf5480f6e2d09278cd3db3c926c1e1bccb387d140da50404b7fd187eb9fdc79c281a0880ca5f'
                                       'ef8679b65e0bda2f6e249d076318063c58913dae8225cd162edda5d76b2040a96064bcce2c32ae4'
                                       'c0627578ab8e7ae8f99a435e1e3a28fd712e04da3cc7f8a7b302e11dd0127dc1291b551ae95c0a1'
                                       '813759c0a78e10d6705f2f68b79ddc8f5c387f8b78c869a3c97274e2221b1551be6c3e9ed08bd24'
                                       'd6232553bc746cb7e8e58432bd5429e8d203c1ac96c6a18097e3a5d2eb5d30d7c5387fc93e54be8'
                                       'facaf3c01b70059b0a411d3b8a78ac4e34be9711df8771cecc365a27a0915dc5ac05951dede527e'
                                       'd8e701af52886ae237bf0a0b109337b1bcc172550ddfb200aeb2bd8493a84ea6a1dca891d720030'
                                       '3ffc880c07d1cf9dac6d1296191fca487f73f9d1e62071c383a003ce39fbd4f7ea5ce82d8a89007'
                                       '3220d440adef42c75be61d52853355f725e41fcf6d45e8918a68ca87addc3b0fd5efa868c7c8bee'
                                       '15242e37b830340598f6f92e9d42d387ca3be199b14da56004ae78a8242352413c733f55744199e'
                                       '640317298a38bbb59bc622baab0ba0ecebc2a92a1d7b12f86263b5e9ed93af36af685cf18dd551a'
                                       '5e084ada8a0148612e86e68636a30a23dbc4fc807a4bd279a0aa7f37d6a0437116c76589e9')
        settings.FACET_SHALLOW_VERIFICATION = True
        response = api_client.put(self.path, encrypted_json, content_type='application/vnd.qabel.noisebox+json')
        assert response.status_code == status.HTTP_204_NO_CONTENT
        # Find this identity
        response = api_client.get(SearchTest.path, {'email': 'test-b24aadf6-7fd9-43b0-86e7-eef9a6d24c65@example.net'})
        assert response.status_code == status.HTTP_200_OK
        result = response.data['identities']
        assert len(result) == 1
        assert result[0]['alias'] == 'Major Anya'
        assert result[0]['public_key'] == '434c0dc39e1dab114b965154c196155bec20071ab75936441565e07f6f9a3022'

    def test_encrypted_failure(self, api_client, settings):
        encrypted_json = bytes.fromhex('cc0330af7d17d21a58f3c277897b1290405960')
        response = api_client.put(self.path, encrypted_json, content_type='application/vnd.qabel.noisebox+json')
        assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_prometheus_metrics(api_client):
    response = api_client.get('/metrics')
    assert response.status_code == 200
    assert b'django_http_requests_latency_seconds' in response.content


class AuthorizationTest:
    APIS = (
        KeyTest.path,
        SearchTest.path,
        UpdateTest.path,
    )

    @pytest.fixture(autouse=True)
    def require_authorization(self, settings):
        settings.REQUIRE_AUTHORIZATION = True

    @pytest.mark.parametrize('api', APIS)
    def test_no_header(self, api_client, api):
        response = api_client.get(api)
        assert response.status_code == 403
        assert response.json()['error'] == 'No authorization supplied.'

    @pytest.mark.parametrize('api', APIS)
    def test_with_invalid_header(self, api_client, api):
        response = api_client.get(api, HTTP_AUTHORIZATION='Token 567')
        assert response.status_code == 403
        assert response.json()['error'] == 'Accounting server unreachable.'

    @pytest.mark.parametrize('api', APIS)
    def test_valid(self, mocker, api_client, api):
        mocker.patch.object(AccountingAuthorization, 'check', lambda self, authorization: (authorization.startswith('Token'), 'All is well'))
        response = api_client.get(api, HTTP_AUTHORIZATION='Token 567')
        assert response.status_code != 403  # It'll usually be no valid request, but it should be authorized.
