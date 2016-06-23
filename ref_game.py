import numpy as np
from scipy.misc import logsumexp

from stanza.monitoring import progress
from stanza.research import config, instance, iterators
from stanza.research.learner import Learner

from neural import sample


class ExhaustiveS1Learner(Learner):
    def __init__(self, base=None):
        options = config.options()
        if base is None:
            self.base = learners.new(options.exhaustive_base_learner)
        else:
            self.base = base

    def train(self, training_instances, validation_instances=None, metrics=None):
        return self.base.train(training_instances=training_instances,
                               validation_instances=validation_instances, metrics=metrics)

    @property
    def num_params(self):
        return self.base.num_params

    def predict_and_score(self, eval_instances, random=False, verbosity=0):
        options = config.options()
        predictions = []
        scores = []

        all_utts = self.base.seq_vec.tokens
        sym_vec = vectorizers.SymbolVectorizer()
        sym_vec.add_all(all_utts)
        prior_scores = self.prior_scores(all_utts)

        base_is_listener = (type(self.base) in listener.LISTENERS.values())

        true_batch_size = options.listener_eval_batch_size / len(all_utts)
        batches = iterators.iter_batches(eval_instances, true_batch_size)
        num_batches = (len(eval_instances) - 1) // true_batch_size + 1

        if options.verbosity + verbosity >= 2:
            print('Testing')
        progress.start_task('Eval batch', num_batches)
        for batch_num, batch in enumerate(batches):
            progress.progress(batch_num)
            batch = list(batch)
            context = len(batch[0].alt_inputs) if batch[0].alt_inputs is not None else 0
            if context:
                output_grid = [(instance.Instance(utt, color)
                                if base_is_listener else
                                instance.Instance(color, utt))
                               for inst in batch for color in inst.alt_inputs
                               for utt in sym_vec.tokens]
                assert len(output_grid) == context * len(batch) * len(all_utts), \
                    'Context must be the same number of colors for all examples'
                true_indices = np.array([inst.input for inst in batch])
            else:
                output_grid = [(instance.Instance(utt, inst.input)
                                if base_is_listener else
                                instance.Instance(inst.input, utt))
                               for inst in batch for utt in sym_vec.tokens]
                true_indices = sym_vec.vectorize_all([inst.input for inst in batch])
                if len(true_indices.shape) == 2:
                    # Sequence vectorizer; we're only using single tokens for now.
                    true_indices = true_indices[:, 0]
            scores = self.base.score(output_grid, verbosity=verbosity)
            if context:
                log_probs = np.array(scores).reshape((len(batch), context, len(all_utts)))
                orig_log_probs = log_probs[np.arange(len(batch)), true_indices, :]
                # Renormalize over only the context colors, and extract the score of
                # the true color.
                log_probs -= logsumexp(log_probs, axis=1)[:, np.newaxis, :]
                log_probs = log_probs[np.arange(len(batch)), true_indices, :]
            else:
                log_probs = np.array(scores).reshape((len(batch), len(all_utts)))
                orig_log_probs = log_probs
            assert log_probs.shape == (len(batch), len(all_utts))
            # Add in the prior scores, if used (S1 \propto L0 * P)
            if prior_scores is not None:
                log_probs = log_probs + 0.5 * prior_scores
            if options.exhaustive_base_weight:
                w = options.exhaustive_base_weight
                log_probs = w * orig_log_probs + (1.0 - w) * log_probs
            # Normalize across utterances. Note that the listener returns probability
            # densities over colors.
            log_probs -= logsumexp(log_probs, axis=1)[:, np.newaxis]
            if random:
                pred_indices = sample(np.exp(log_probs))
            else:
                pred_indices = np.argmax(log_probs, axis=1)
            predictions.extend(sym_vec.unvectorize_all(pred_indices))
            scores.extend(log_probs[np.arange(len(batch)), true_indices].tolist())
        progress.end_task()

        return predictions, scores

    def dump(self, outfile):
        return self.base.dump(outfile)

    def load(self, infile):
        return self.base.load(infile)

    def prior_scores(self, utts):
        # Don't use prior scores by default
        pass


class ExhaustiveS1PriorLearner(ExhaustiveS1Learner):
    def __init__(self, prior_counter, base=None):
        self.prior_counter = prior_counter
        self.denominator = sum(prior_counter.values())
        super(ExhaustiveS1PriorLearner, self).__init__(base=base)

    def prior_scores(self, utts):
        return np.log(np.array([self.prior_counter[u] for u in utts])) - np.log(self.denominator)


class DirectRefGameLearner(Learner):
    def __init__(self, base=None):
        options = config.options()
        if base is None:
            self.base = learners.new(options.direct_base_learner)
        else:
            self.base = base

    def train(self, training_instances, validation_instances=None, metrics=None):
        return self.base.train(training_instances=training_instances,
                               validation_instances=validation_instances, metrics=metrics)

    @property
    def num_params(self):
        return self.base.num_params

    def predict_and_score(self, eval_instances, random=False, verbosity=0):
        options = config.options()
        predictions = []
        scores = []
        base_is_listener = (type(self.base) in listener.LISTENERS.values())
        assert options.listener, 'Eval data should be listener data for DirectRefGameLearner'

        true_batch_size = options.listener_eval_batch_size / options.num_distractors
        batches = iterators.iter_batches(eval_instances, true_batch_size)
        num_batches = (len(eval_instances) - 1) // true_batch_size + 1

        if options.verbosity + verbosity >= 2:
            print('Testing')
        progress.start_task('Eval batch', num_batches)
        for batch_num, batch in enumerate(batches):
            progress.progress(batch_num)
            batch = list(batch)
            assert batch[0].alt_outputs, 'No context given for direct listener testing'
            context = len(batch[0].alt_outputs)
            output_grid = [instance.Instance(inst.input, color)
                           if base_is_listener else
                           instance.Instance(color, inst.input)
                           for inst in batch for color in inst.alt_outputs]
            assert len(output_grid) == context * len(batch), \
                'Context must be the same number of colors for all examples'
            true_indices = np.array([inst.output for inst in batch])
            grid_scores = self.base.score(output_grid, verbosity=verbosity)
            log_probs = np.array(grid_scores).reshape((len(batch), context))
            # Renormalize over only the context colors, and extract the score of
            # the true color.
            log_probs -= logsumexp(log_probs, axis=1)[:, np.newaxis]
            assert log_probs.shape == (len(batch), context)
            pred_indices = np.argmax(log_probs, axis=1)
            predictions.extend(pred_indices.tolist())
            scores.extend(log_probs[np.arange(len(batch)), true_indices].tolist())
        progress.end_task()

        return predictions, scores

    def dump(self, outfile):
        return self.base.dump(outfile)

    def load(self, infile):
        return self.base.load(infile)


import learners
import listener
import vectorizers


parser = config.get_options_parser()
parser.add_argument('--exhaustive_base_learner', default='Listener',
                    choices=learners.LEARNERS.keys(),
                    help='The name of the model to use as the L0 for exhaustive enumeration-based '
                         'speaker models.')
parser.add_argument('--exhaustive_base_weight', default=0.0, type=float,
                    help='Weight given to the base agent for the exhaustive RSA model. The RSA '
                         "agent's weight will be 1 - exhaustive_base_weight.")
parser.add_argument('--direct_base_learner', default='Listener',
                    choices=learners.LEARNERS.keys(),
                    help='The name of the model to use as the level-0 agent for direct score-based '
                         'listener models.')
