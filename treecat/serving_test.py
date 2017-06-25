from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools

import numpy as np
import pytest

from treecat.serving import make_posterior
from treecat.serving import serve_model
from treecat.testutil import TINY_CONFIG
from treecat.testutil import TINY_DATA
from treecat.testutil import TINY_MASK
from treecat.training import train_model


@pytest.fixture(scope='module')
def model():
    return train_model(TINY_DATA, TINY_MASK, TINY_CONFIG)


def test_make_posterior(model):
    grid = model['tree'].tree_grid
    suffstats = model['suffstats']
    factors = make_posterior(grid, suffstats)
    observed = factors['observed']
    observed_latent = factors['observed_latent']
    latent = factors['latent']
    latent_latent = factors['latent_latent']

    # Check shape.
    N, V = TINY_DATA.shape
    E = V - 1
    C = TINY_CONFIG['num_categories']
    M = TINY_CONFIG['num_clusters']
    assert observed.shape == (V, C)
    assert observed_latent.shape == (V, C, M)
    assert latent.shape == (V, M)
    assert latent_latent.shape == (E, M, M)

    # Check normalization.
    atol = 1e-5
    assert np.allclose(observed.sum(1), 1.0, atol=atol)
    assert np.allclose(observed_latent.sum((1, 2)), 1.0, atol=atol)
    assert np.allclose(latent.sum(1), 1.0, atol=atol)
    assert np.allclose(latent_latent.sum((1, 2)), 1.0, atol=atol)

    # Check marginals.
    assert np.allclose(observed_latent.sum(2), observed, atol=atol)
    assert np.allclose(observed_latent.sum(1), latent, atol=atol)
    assert np.allclose(latent_latent.sum(2), latent[grid[1, :], :], atol=atol)
    assert np.allclose(latent_latent.sum(1), latent[grid[2, :], :], atol=atol)


@pytest.mark.parametrize('engine', [
    'numpy',
    'tensorflow',
])
def test_server_init(engine, model):
    config = TINY_CONFIG.copy()
    config['engine'] = engine
    serve_model(model['tree'], model['suffstats'], config)


@pytest.mark.parametrize('engine', [
    'numpy',
    'tensorflow',
])
def test_server_sample_shape(engine, model):
    config = TINY_CONFIG.copy()
    config['engine'] = engine
    server = serve_model(model['tree'], model['suffstats'], config)

    # Sample all possible mask patterns.
    N, V = TINY_DATA.shape
    factors = [[True, False]] * V
    for mask in itertools.product(*factors):
        mask = np.array(mask, dtype=np.bool_)
        samples = server.sample(TINY_DATA, mask)
        assert samples.shape == TINY_DATA.shape
        assert samples.dtype == TINY_DATA.dtype
        assert np.allclose(samples[:, mask], TINY_DATA[:, mask])


@pytest.mark.parametrize('engine', [
    'numpy',
    'tensorflow',
])
def test_server_logprob_shape(engine, model):
    config = TINY_CONFIG.copy()
    config['engine'] = engine
    server = serve_model(model['tree'], model['suffstats'], config)

    # Sample all possible mask patterns.
    N, V = TINY_DATA.shape
    factors = [[True, False]] * V
    for mask in itertools.product(*factors):
        mask = np.array(mask, dtype=np.bool_)
        logprob = server.logprob(TINY_DATA, mask)
        assert logprob.shape == (N, )
        assert np.isfinite(logprob).all()


@pytest.mark.parametrize('engine', [
    pytest.mark.xfail('numpy'),
    pytest.mark.xfail('tensorflow'),
])
def test_server_logprob_negative(engine, model):
    config = TINY_CONFIG.copy()
    config['engine'] = engine
    server = serve_model(model['tree'], model['suffstats'], config)

    # Sample all possible mask patterns.
    N, V = TINY_DATA.shape
    factors = [[True, False]] * V
    for mask in itertools.product(*factors):
        mask = np.array(mask, dtype=np.bool_)
        logprob = server.logprob(TINY_DATA, mask)
        assert (logprob < 0.0).all()  # Assuming features are discrete.


@pytest.mark.parametrize('engine', [
    pytest.mark.xfail('numpy'),
    pytest.mark.xfail('tensorflow'),
])
def test_server_logprob_is_normalized(engine, model):
    config = TINY_CONFIG.copy()
    config['engine'] = engine
    server = serve_model(model['tree'], model['suffstats'], config)

    # The total probability of all possible rows should be 1.
    C = config['num_categories']
    N, V = TINY_DATA.shape
    factors = [range(C)] * V
    data = np.array(list(itertools.product(*factors)), dtype=np.int32)
    mask = np.array([True] * V, dtype=np.bool_)
    logprob = server.logprob(data, mask)
    logtotal = np.logaddexp.reduce(logprob)
    assert abs(logtotal) < 1e-6, logtotal
