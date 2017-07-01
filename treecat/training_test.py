from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import pytest
from goftests import multinomial_goodness_of_fit

from treecat.config import DEFAULT_CONFIG
from treecat.generate import generate_dataset
from treecat.structure import TreeStructure
from treecat.testutil import TINY_CONFIG
from treecat.training import TreeCatTrainer
from treecat.training import get_annealing_schedule
from treecat.training import sample_from_probs
from treecat.training import train_model


@pytest.mark.parametrize('size', range(1, 10))
def test_sample_from_probs(size):
    np.random.seed(size)
    probs = np.exp(2 * np.random.random(size)).astype(np.float32)
    probs /= probs.sum()
    counts = np.zeros(size, dtype=np.int32)
    num_samples = 2000 * size
    for _ in range(num_samples):
        counts[sample_from_probs(probs)] += 1
    print(counts)
    print(probs * num_samples)
    gof = multinomial_goodness_of_fit(probs, counts, num_samples, plot=True)
    assert 1e-2 < gof


def test_get_annealing_schedule():
    np.random.seed(0)
    num_rows = 10
    schedule = get_annealing_schedule(num_rows, TINY_CONFIG)
    for step, (action, row_id) in enumerate(schedule):
        assert step < 1000
        assert action in ['add_row', 'remove_row', 'sample_tree']
        if action == 'sample_tree':
            assert row_id is None
        else:
            assert 0 <= row_id and row_id < num_rows


@pytest.mark.parametrize('N,V,C,M', [
    (1, 1, 1, 1),
    (2, 2, 2, 2),
    (3, 3, 3, 3),
    (4, 4, 4, 4),
    (5, 5, 5, 5),
    (6, 6, 6, 6),
])
def test_train_model(N, V, C, M):
    config = DEFAULT_CONFIG.copy()
    config['model_num_clusters'] = M
    data = generate_dataset(num_rows=N, num_cols=V, num_cats=C)
    model = train_model(data, config)

    assert model['config'] == config
    assert isinstance(model['tree'], TreeStructure)
    grid = model['tree'].tree_grid
    assignments = model['assignments']
    vert_ss = model['suffstats']['vert_ss']
    edge_ss = model['suffstats']['edge_ss']
    feat_ss = model['suffstats']['feat_ss']

    # Check shape.
    V = len(data)
    N = data[0].shape[0]
    E = V - 1
    M = config['model_num_clusters']
    assert grid.shape == (3, E)
    assert assignments.shape == (N, V)
    assert vert_ss.shape == (V, M)
    assert edge_ss.shape == (E, M, M)
    assert len(feat_ss) == V
    for v in range(V):
        assert feat_ss[v].shape == (data[v].shape[1], M)

    # Check bounds.
    assert np.all(0 <= vert_ss)
    assert np.all(vert_ss <= N)
    assert np.all(0 <= edge_ss)
    assert np.all(edge_ss <= N)
    assert np.all(0 <= assignments)
    assert np.all(assignments < M)
    for v in range(V):
        assert np.all(0 <= feat_ss[v])

    # Check marginals.
    assert vert_ss.sum() == N * V
    assert np.all(vert_ss.sum(1) == N)
    assert edge_ss.sum() == N * E
    assert np.all(edge_ss.sum((1, 2)) == N)
    assert np.all(edge_ss.sum(2) == vert_ss[grid[1, :]])
    assert np.all(edge_ss.sum(1) == vert_ss[grid[2, :]])
    for v in range(V):
        assert feat_ss[v].sum() == data[v].sum()
        assert np.all(feat_ss[v].sum(1) == data[v].sum(0))

    # Check computation from scratch.
    for v in range(V):
        counts = np.bincount(assignments[:, v], minlength=M)
        assert np.all(vert_ss[v, :] == counts)
    for e, v1, v2 in grid.T:
        pairs = assignments[:, v1].astype(np.int32) * M + assignments[:, v2]
        counts = np.bincount(pairs, minlength=M * M).reshape((M, M))
        assert np.all(edge_ss[e, :, :] == counts)
    for v in range(V):
        counts = np.zeros_like(feat_ss[v])
        for n in range(N):
            counts[:, assignments[n, v]] += data[v][n, :]
        assert np.all(feat_ss[v] == counts)


def hash_assignments(assignments):
    assert isinstance(assignments, np.ndarray)
    return tuple(tuple(row) for row in assignments)


@pytest.mark.parametrize('N,V,C,M', [
    (1, 1, 1, 1),
    (1, 2, 2, 2),
    (1, 2, 2, 3),
    (1, 3, 2, 2),
    (1, 4, 2, 2),
    pytest.mark.xfail((2, 1, 1, 2)),
    (2, 1, 2, 2),
    pytest.mark.xfail((2, 1, 2, 3)),
    pytest.mark.xfail((2, 2, 1, 2)),
    (2, 2, 2, 2),
    (2, 3, 2, 2),
    pytest.mark.xfail((3, 1, 1, 2)),
    (3, 1, 2, 2),
    pytest.mark.xfail((4, 1, 1, 2)),
])
def test_assignment_sampler_gof(N, V, C, M):
    config = DEFAULT_CONFIG.copy()
    config['learning_sample_tree_steps'] = 0  # Disable tree kernel.
    config['model_num_clusters'] = M
    data = generate_dataset(num_rows=N, num_cols=V, num_cats=C)
    trainer = TreeCatTrainer(data, config)
    print('Data:')
    for col in data:
        print(col)

    # Add all rows.
    for row_id in range(N):
        trainer.add_row(row_id)

    # Collect samples.
    num_samples = 100 * M**(N * V)
    counts = {}
    logprobs = {}
    for _ in range(num_samples):
        for row_id in range(N):
            # This is a single-site Gibbs sampler.
            trainer.remove_row(row_id)
            trainer.add_row(row_id)
        key = hash_assignments(trainer.assignments)
        if key in counts:
            counts[key] += 1
        else:
            counts[key] = 1
            logprobs[key] = trainer.logprob()
    assert len(counts) == M**(N * V)

    # Check accuracy using Pearson's chi-squared test.
    keys = sorted(counts.keys())
    counts = np.array([counts[k] for k in keys], dtype=np.int32)
    probs = np.exp(np.array([logprobs[k] for k in keys]))
    probs /= probs.sum()
    print('Actual\tExpected\tAssignment')
    for count, prob, key in zip(counts, probs, keys):
        print('{:}\t{:0.1f}\t{}'.format(count, prob * num_samples, key))
    gof = multinomial_goodness_of_fit(probs, counts, num_samples, plot=True)
    assert 1e-2 < gof
