from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
from collections import deque

import numpy as np

from treecat.util import COUNTERS
from treecat.util import HISTOGRAMS
from treecat.util import profile
from treecat.util import sample_from_probs

logger = logging.getLogger(__name__)


class TreeStructure(object):
    """Topological data representing a tree on features."""

    def __init__(self, num_vertices):
        logger.debug('TreeStructure with %d vertices', num_vertices)
        self._num_vertices = num_vertices
        self._num_edges = num_vertices - 1
        self.set_edges([(v, v + 1) for v in range(num_vertices - 1)])
        self._complete_grid = None  # Lazily constructed.
        self._vertices = np.arange(num_vertices, dtype=np.int32)

    def __eq__(self, other):
        return (self._num_vertices == other._num_vertices and
                (self._tree_grid == other._tree_grid).all())

    def set_edges(self, edges):
        """Sets the edges of this tree.

        Args:
          edges: A list of (vertex, vertex) pairs.
        """
        assert len(edges) == self._num_edges
        self._tree_grid = make_tree(edges)

    @property
    def num_vertices(self):
        return self._num_vertices

    @property
    def num_edges(self):
        return self._num_edges

    @property
    def tree_grid(self):
        """Array of (edge, vertex, vertex) triples defining the tree graph."""
        return self._tree_grid

    @property
    def complete_grid(self):
        """Array of (edge,vertex,vertex) triples defining a complete graph."""
        if self._complete_grid is None:
            self._complete_grid = make_complete_graph(self._num_vertices)
        return self._complete_grid

    @property
    def vertices(self):
        return self._vertices

    def gc(self):
        """Garbage collect temporary cached data structures."""
        self._complete_grid = None


def find_complete_edge(v1, v2):
    """Find the edge index k of an unsorted pair of vertices (v1, v2)."""
    if v2 < v1:
        v1, v2 = v2, v1
    return v1 + v2 * (v2 - 1) // 2


def make_complete_graph(num_vertices):
    """Constructs a complete graph.

    The pairing function is: k = v1 + v2 * (v2 - 1) // 2

    Args:
      num_vertices: Number of vertices.

    Returns: A tuple with elements:
      V: Number of vertices.
      K: Number of edges.
      grid: a 3 x K grid of (edge, vertex, vertex) triples.
    """
    V = num_vertices
    K = V * (V - 1) // 2
    grid = np.zeros([3, K], np.int32)
    k = 0
    for v2 in range(V):
        for v1 in range(v2):
            grid[:, k] = [k, v1, v2]
            k += 1
    return grid


def make_tree(edges):
    """Constructs a tree graph from a set of (vertex,vertex) pairs.

    Args:
      edges: A list or set of unordered (vertex, vertex) pairs.

    Returns: A tuple with elements:
      V: Number of vertices.
      E: Number of edges.
      grid: a 3 x E grid of (edge, vertex, vertex) triples.
    """
    assert all(isinstance(edge, tuple) for edge in edges)
    edges = [tuple(sorted(edge)) for edge in edges]
    edges.sort()
    E = len(edges)
    grid = np.zeros([3, E], np.int32)
    for e, (v1, v2) in enumerate(edges):
        grid[:, e] = [e, v1, v2]
    return grid


def find_center_of_tree(grid):
    """Finds a maximally central vertex in a tree graph.

    Args:
        grid: A tree graph as returned by make_tree().

    Returns:
        Vertex id of a maximally central vertex.
    """
    E = grid.shape[1]
    V = 1 + E
    neighbors = [set() for _ in range(V)]
    for e, v1, v2 in grid.T:
        neighbors[v1].add(v2)
        neighbors[v2].add(v1)
    queue = deque()
    for v in reversed(range(V)):
        if len(neighbors[v]) <= 1:
            queue.append(v)
    while queue:
        v = queue.popleft()
        for v2 in sorted(neighbors[v], reverse=True):
            neighbors[v2].remove(v)
            if len(neighbors[v2]) == 1:
                queue.append(v2)
    return v


# Op codes.
OP_UP = 0
OP_IN = 1
OP_ROOT = 2
OP_OUT = 3


def make_propagation_schedule(grid, root=None):
    """Makes an efficient schedule for message passing on a tree.

    Args:
      grid: A tree graph as returned by make_tree().
      root: Optional root vertex, defaults to find_center_of_tree(grid).

    Returns:
      A numpy array with rows (opcode, vertex, relative, edge), where
      opcode: One of 0 = up, 1 = in, 2 = root, 3 = out.
      vertex: The vertex id of the vertex being operated on.
      relative: The vertex ide of a relative, either a parent or child.
      edge: The edge id of the (vertex, relative) pair.
    """
    if root is None:
        root = find_center_of_tree(grid)
    E = grid.shape[1]
    V = 1 + E
    neighbors = [set() for _ in range(V)]
    edge_dict = {}
    for e, v1, v2 in grid.T:
        neighbors[v1].add(v2)
        neighbors[v2].add(v1)
        edge_dict[v1, v2] = e
        edge_dict[v2, v1] = e

    # Construct a nested schedule.
    nested_schedule = []
    queue = deque()
    queue.append((root, None))
    while queue:
        v, parent = queue.popleft()
        nested_schedule.append((v, parent, []))
        for v2 in sorted(neighbors[v]):
            if v2 != parent:
                queue.append((v2, v))
    for v, parent, children in nested_schedule:
        for v2 in sorted(neighbors[v]):
            if v2 != parent:
                children.append(v2)

    # Construct a flattened schedule.
    schedule = np.zeros([V + E + V, 4], np.int16)
    pos = 0
    for v, parent, children in reversed(nested_schedule):
        schedule[pos, :] = [OP_UP, v, 0, 0]
        pos += 1
        for child in children:
            schedule[pos, :] = [OP_IN, v, child, edge_dict[v, child]]
            pos += 1
    schedule[pos, :] = [OP_ROOT, v, 0, 0]
    pos += 1
    for v, parent, children in nested_schedule[1:]:
        schedule[pos, :] = [OP_OUT, v, parent, edge_dict[v, parent]]
        pos += 1
    assert pos == V + E + 1 + E

    return schedule


class MutableTree(object):
    """MCMC tree for random spanning trees."""

    __slots__ = ['VEK', 'grid', 'e2k', 'k2e', 'neighbors', 'components']

    def __init__(self, grid, edges):
        """Build a mutable spanning tree.

        Args:
          grid: A 3 x K array as returned by make_complete_graph().
          edges: A list of E edges in the form of (vertex,vertex) pairs.
        """
        E = len(edges)
        V = 1 + E
        K = V * (V - 1) // 2
        assert grid.shape == (3, K)
        self.VEK = (V, E, K)
        self.grid = grid
        self.k2e = {}
        self.e2k = {}
        self.neighbors = [set() for _ in range(V)]
        for e, (v1, v2) in enumerate(edges):
            k = find_complete_edge(v1, v2)
            self.k2e[k] = e
            self.e2k[e] = k
            self.neighbors[v1].add(v2)
            self.neighbors[v2].add(v1)
        self.components = np.zeros([V], dtype=np.bool_)
        assert len(self.e2k) == self.VEK[1]
        assert len(self.k2e) == self.VEK[1]

    @profile
    def remove_edge(self, e):
        """Remove edge at position e from tree and update data structures."""
        assert len(self.e2k) == self.VEK[1]
        assert len(self.k2e) == self.VEK[1]
        neighbors = self.neighbors
        components = self.components
        k = self.e2k.pop(e)
        self.k2e.pop(k)
        v1, v2 = self.grid[1:, k]
        neighbors[v1].remove(v2)
        neighbors[v2].remove(v1)
        stack = [v1]
        while stack:
            v1 = stack.pop()
            components[v1] = True
            for v2 in neighbors[v1]:
                if not components[v2]:
                    stack.append(v2)
        assert len(self.e2k) == self.VEK[1] - 1
        assert len(self.k2e) == self.VEK[1] - 1
        return k

    def add_edge(self, e, k):
        """Add edge k at location e and update data structures."""
        assert len(self.e2k) == self.VEK[1] - 1
        assert len(self.k2e) == self.VEK[1] - 1
        v1, v2 = self.grid[1:, k]
        assert self.components[v1] != self.components[v2]
        self.k2e[k] = e
        self.e2k[e] = k
        self.neighbors[v1].add(v2)
        self.neighbors[v2].add(v1)
        self.components[:] = False
        assert len(self.e2k) == self.VEK[1]
        assert len(self.k2e) == self.VEK[1]


@profile
def sample_tree(grid, edge_logits, edges, steps=1):
    """Sample a random spanning tree of a dense weighted graph using MCMC.

    This uses Gibbs sampling on edges. Consider E undirected edges that can
    move around a graph of V=1+E vertices. The edges are constrained so that no
    two edges can span the same pair of vertices and so that the edges must
    form a spanning tree. To Gibbs sample, chose one of the E edges at random
    and move it anywhere else in the graph. After we remove the edge, notice
    that the graph is split into two connected components. The constraints
    imply that the edge must be replaced so as to connect the two components.
    Hence to Gibbs sample, we collect all such bridging (vertex,vertex) pairs
    and sample from them in proportion to exp(edge_logits).

    Args:
      grid: A 3 x K array as returned by make_complete_graph().
      edge_logits: A length-K array of nonnormalized log probabilities.
      edges: A list of E initial edges in the form of (vertex,vertex) pairs.
      steps: Number of MCMC steps to take.

    Returns:
      A list of (vertex, vertex) pairs.
    """
    logger.debug('sample_tree sampling a random spanning tree')
    COUNTERS.sample_tree_calls += 1
    if len(edges) <= 1:
        return edges
    tree = MutableTree(grid, edges)
    V, E, K = tree.VEK

    for step in range(steps):
        for e in range(E):
            e = np.random.randint(E)  # Sequential scanning doesn't work.
            k1 = tree.remove_edge(e)
            valid_edges = np.where(
                tree.components[grid[1, :]] != tree.components[grid[2, :]])[0]
            valid_probs = edge_logits[valid_edges]
            valid_probs -= valid_probs.max()
            np.exp(valid_probs, out=valid_probs)
            total_prob = valid_probs.sum()
            if total_prob > 0:
                valid_probs *= 0.9999995 / total_prob  # Avoid np.binom errors.
                k2 = valid_edges[sample_from_probs(valid_probs)]
            else:
                k2 = k1
                COUNTERS.sample_tree_infeasible += 1
            tree.add_edge(e, k2)

            COUNTERS.sample_tree_propose += 1
            COUNTERS.sample_tree_accept += (k1 != k2)
            HISTOGRAMS.sample_tree_log2_choices.update(
                [len(valid_edges).bit_length()])

    edges = sorted((grid[1, k], grid[2, k]) for k in tree.e2k.values())
    assert len(edges) == E
    return edges
