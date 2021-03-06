from baseline.tf.tfy import *
from baseline.utils import listify
from baseline.reporting import basic_reporting
from baseline.train import Trainer


class LanguageModelTrainerTf(Trainer):

    def __init__(self, model, **kwargs):
        super(LanguageModelTrainerTf, self).__init__()
        self.model = model
        self.loss = model.create_loss()
        self.global_step, self.train_op = optimizer(self.loss, **kwargs)

    def checkpoint(self):
        self.model.saver.save(self.model.sess, "./tf-checkpoints/lm", global_step=self.global_step)

    def recover_last_checkpoint(self):
        latest = tf.train.latest_checkpoint("./tf-checkpoints")
        print("Reloading " + latest)
        self.model.saver.restore(self.model.sess, latest)

    def train(self, ts, reporting_fns):
        total_loss = 0.0
        iters = 0
        state = self.model.sess.run(self.model.initial_state)

        fetches = {
            "loss": self.loss,
            "final_state": self.model.final_state,
        }

        fetches["train_op"] = self.train_op
        fetches["global_step"] = self.global_step

        step = 0
        metrics = {}

        for x, xch, y in ts:

            feed_dict = self.model.make_feed_dict(x, xch, y, True)
            for i, (c, h) in enumerate(self.model.initial_state):
                feed_dict[c] = state[i].c
                feed_dict[h] = state[i].h

            vals = self.model.sess.run(fetches, feed_dict)
            loss = vals["loss"]
            state = vals["final_state"]
            global_step = vals["global_step"]
            total_loss += loss
            iters += self.model.nbptt
            step += 1
            if step % 500 == 0:
                metrics['avg_loss'] = total_loss / iters
                metrics['perplexity'] = np.exp(total_loss / iters)
                for reporting in reporting_fns:
                    reporting(metrics, global_step, 'Train')

        metrics['avg_loss'] = total_loss / iters
        metrics['perplexity'] = np.exp(total_loss / iters)

        for reporting in reporting_fns:
            reporting(metrics, global_step, 'Train')
        return metrics

    def test(self, ts, reporting_fns, phase):
        total_loss = 0.0
        iters = 0
        epochs = 0
        if phase == 'Valid':
            self.valid_epochs += 1
            epochs = self.valid_epochs

        state = self.model.sess.run(self.model.initial_state)

        fetches = {
            "loss": self.loss,
            "final_state": self.model.final_state,
        }

        step = 0
        metrics = {}

        for x, xch, y in ts:

            feed_dict = self.model.make_feed_dict(x, xch, y, False)
            for i, (c, h) in enumerate(self.model.initial_state):
                feed_dict[c] = state[i].c
                feed_dict[h] = state[i].h

            vals = self.model.sess.run(fetches, feed_dict)
            loss = vals["loss"]
            state = vals["final_state"]
            total_loss += loss
            iters += self.model.nbptt
            step += 1

        metrics['avg_loss'] = total_loss / iters
        metrics['perplexity'] = np.exp(total_loss / iters)

        for reporting in reporting_fns:
            reporting(metrics, epochs, phase)
        return metrics


def fit(model, ts, vs, es=None, **kwargs):
    epochs = int(kwargs['epochs']) if 'epochs' in kwargs else 5
    patience = int(kwargs['patience']) if 'patience' in kwargs else epochs

    model_file = kwargs['outfile'] if 'outfile' in kwargs and kwargs['outfile'] is not None else './seq2seq-model-tf'
    after_train_fn = kwargs['after_train_fn'] if 'after_train_fn' in kwargs else None
    trainer = LanguageModelTrainerTf(model, **kwargs)
    init = tf.global_variables_initializer()
    model.sess.run(init)
    saver = tf.train.Saver()
    model.save_using(saver)

    do_early_stopping = bool(kwargs.get('do_early_stopping', True))

    if do_early_stopping:
        early_stopping_metric = kwargs.get('early_stopping_metric', 'avg_loss')
        patience = kwargs.get('patience', epochs)
        print('Doing early stopping on [%s] with patience [%d]' % (early_stopping_metric, patience))

    reporting_fns = listify(kwargs.get('reporting', basic_reporting))
    print('reporting', reporting_fns)

    min_metric = 10000
    last_improved = 0

    for epoch in range(epochs):

        trainer.train(ts, reporting_fns)
        if after_train_fn is not None:
            after_train_fn(model)

        test_metrics = trainer.test(vs, reporting_fns, phase='Valid')

        if do_early_stopping is False:
            trainer.checkpoint()
            trainer.model.save(model_file)

        elif test_metrics[early_stopping_metric] < min_metric:
            last_improved = epoch
            min_metric = test_metrics[early_stopping_metric]
            print('New min %.3f' % min_metric)
            trainer.checkpoint()
            trainer.model.save(model_file)

        elif (epoch - last_improved) > patience:
            print('Stopping due to persistent failures to improve')
            break

    if do_early_stopping is True:
        print('Best performance on min_metric %.3f at epoch %d' % (min_metric, last_improved))
    if es is not None:
        trainer.recover_last_checkpoint()
        trainer.test(es, reporting_fns, phase='Test')


