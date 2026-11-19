import time
from http import HTTPStatus
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from academico import turma_client
from academico.turma_client import (
    TIMEOUT_SEGUNDOS,
    buscar_aluno_por_matricula,
    buscar_aluno_por_usuario,
    consultar_turmas_em_paralelo,
    listar_ofertas,
)


def resposta(status_code=HTTPStatus.OK, payload=None):
    retorno = Mock()
    retorno.status_code = status_code
    retorno.is_error = status_code >= HTTPStatus.BAD_REQUEST
    retorno.json.return_value = payload
    return retorno


def test_buscar_aluno_por_usuario_encaminha_o_token():
    usuario_id = uuid4()

    with patch.object(
        turma_client._cliente_http,
        'get',
        return_value=resposta(payload={'id': str(uuid4())}),
    ) as requisicao:
        buscar_aluno_por_usuario(usuario_id, 'token-do-usuario')

    cabecalhos = requisicao.call_args.kwargs['headers']
    assert cabecalhos == {'Authorization': 'Bearer token-do-usuario'}
    assert requisicao.call_args.args[0].endswith(
        f'/alunos/usuario_id/{usuario_id}'
    )


def test_listar_ofertas_encaminha_o_token_em_cada_pagina():
    with patch.object(
        turma_client._cliente_http,
        'get',
        return_value=resposta(payload={'turmas': []}),
    ) as requisicao:
        listar_ofertas({'curso_id': 'x'}, 'token-do-usuario')

    assert requisicao.call_args.kwargs['headers'] == {
        'Authorization': 'Bearer token-do-usuario'
    }


def test_buscar_aluno_por_matricula_escapa_o_valor():
    with patch.object(
        turma_client._cliente_http,
        'get',
        return_value=resposta(payload={}),
    ) as requisicao:
        buscar_aluno_por_matricula('2026/1?x', 'token')

    assert requisicao.call_args.args[0].endswith(
        '/alunos/matricula_id/2026%2F1%3Fx'
    )


def test_timeout_vira_504():
    with patch.object(
        turma_client._cliente_http,
        'get',
        side_effect=httpx.TimeoutException('lento'),
    ), pytest.raises(HTTPException) as excinfo:
        buscar_aluno_por_usuario(uuid4(), 'token')

    assert excinfo.value.status_code == HTTPStatus.GATEWAY_TIMEOUT


def test_falha_de_conexao_vira_503():
    with patch.object(
        turma_client._cliente_http,
        'get',
        side_effect=httpx.ConnectError('recusado'),
    ), pytest.raises(HTTPException) as excinfo:
        buscar_aluno_por_usuario(uuid4(), 'token')

    assert excinfo.value.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_aluno_inexistente_vira_404():
    with patch.object(
        turma_client._cliente_http,
        'get',
        return_value=resposta(status_code=HTTPStatus.NOT_FOUND),
    ), pytest.raises(HTTPException) as excinfo:
        buscar_aluno_por_usuario(uuid4(), 'token')

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND


TAMANHO_PAGINA_TESTE = 100


def test_listar_ofertas_pagina_ate_o_fim():
    primeira = [{'id': str(uuid4())} for _ in range(TAMANHO_PAGINA_TESTE)]
    segunda = [{'id': str(uuid4())}]

    with patch.object(
        turma_client._cliente_http,
        'get',
        side_effect=[
            resposta(payload={'turmas': primeira}),
            resposta(payload={'turmas': segunda}),
        ],
    ) as requisicao:
        turmas = listar_ofertas({'curso_id': 'x'}, 'token')

    assert len(turmas) == TAMANHO_PAGINA_TESTE + 1
    assert requisicao.call_count == 2 * 1


def test_permissao_negada_no_turma_vira_403():
    with patch.object(
        turma_client._cliente_http,
        'get',
        return_value=resposta(status_code=HTTPStatus.FORBIDDEN),
    ), pytest.raises(HTTPException) as excinfo:
        buscar_aluno_por_matricula('2026001', 'token')

    assert excinfo.value.status_code == HTTPStatus.FORBIDDEN


def test_cliente_http_persistente_reutilizado_com_timeout_25s():
    timeout_esperado = 25.0
    chamadas_esperadas = 2
    assert TIMEOUT_SEGUNDOS == timeout_esperado
    assert isinstance(turma_client._cliente_http, httpx.Client)

    cliente_inicial = turma_client._cliente_http
    with patch.object(
        turma_client._cliente_http,
        'get',
        return_value=resposta(payload={'id': str(uuid4())}),
    ) as requisicao:
        buscar_aluno_por_usuario(uuid4(), 'token-1')
        buscar_aluno_por_matricula('2026001', 'token-2')

    assert turma_client._cliente_http is cliente_inicial
    assert requisicao.call_count == chamadas_esperadas
    assert requisicao.call_args.kwargs['timeout'] == timeout_esperado


def test_consultar_turmas_em_paralelo_lista_vazia():
    assert consultar_turmas_em_paralelo([], 'token') == []


def test_consultar_turmas_em_paralelo_preserva_ordem():
    def fake_get(url, **_kwargs):
        if url.endswith('/turma-1'):
            time.sleep(0.05)
            return resposta(payload={'ordem': 1})
        if url.endswith('/turma-2'):
            time.sleep(0.01)
            return resposta(payload={'ordem': 2})
        return resposta(payload={'ordem': 3})

    with patch.object(
        turma_client._cliente_http,
        'get',
        side_effect=fake_get,
    ):
        resultado = consultar_turmas_em_paralelo(
            ['/turma-1', '/turma-2', '/turma-3'], 'token'
        )

    assert resultado == [{'ordem': 1}, {'ordem': 2}, {'ordem': 3}]


@pytest.mark.parametrize(
    ('efeito', 'status_esperado'),
    [
        (resposta(status_code=HTTPStatus.FORBIDDEN), HTTPStatus.FORBIDDEN),
        (
            resposta(status_code=HTTPStatus.INTERNAL_SERVER_ERROR),
            HTTPStatus.BAD_GATEWAY,
        ),
        (httpx.ConnectError('recusado'), HTTPStatus.SERVICE_UNAVAILABLE),
        (httpx.TimeoutException('lento'), HTTPStatus.GATEWAY_TIMEOUT),
    ],
)
def test_consultar_turmas_em_paralelo_propaga_erros(efeito, status_esperado):
    def fake_get(url, **_kwargs):
        if url.endswith('/turma-2'):
            if isinstance(efeito, Exception):
                raise efeito
            return efeito
        return resposta(payload={'ok': True})

    with patch.object(
        turma_client._cliente_http,
        'get',
        side_effect=fake_get,
    ), pytest.raises(HTTPException) as excinfo:
        consultar_turmas_em_paralelo(['/turma-1', '/turma-2'], 'token')

    assert excinfo.value.status_code == status_esperado
