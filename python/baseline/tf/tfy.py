import tensorflow as tf
import numpy as np
from tensorflow.python.ops import rnn_cell_impl
import math

from tensorflow.python.layers import core as layers_core
from baseline.utils import lookup_sentence, beam_multinomial

def tensor2seq(tensor):
    return tf.unstack(tf.transpose(tensor, perm=[1, 0, 2]))


def seq2tensor(sequence):
    return tf.transpose(tf.stack(sequence), perm=[1, 0, 2])


# Method for seq2seq w/ attention using TF's library
def legacy_attn_rnn_seq2seq(encoder_inputs,
                            decoder_inputs,
                            cell,
                            num_heads=1,
                            dtype=tf.float32,
                            scope=None):
    with tf.variable_scope(scope or "attention_rnn_seq2seq"):
        encoder_outputs, enc_state = tf.contrib.rnn.static_rnn(cell, encoder_inputs, dtype=dtype)
        top_states = [tf.reshape(e, [-1, 1, cell.output_size])
                      for e in encoder_outputs]
        attention_states = tf.concat(values=top_states, axis=1)
    
    return tf.contrib.legacy_seq2seq.attention_decoder(decoder_inputs,
                                                       enc_state,
                                                       attention_states,
                                                       cell,
                                                       num_heads=num_heads)


def dense_layer(output_layer_depth):
    output_layer = layers_core.Dense(output_layer_depth, use_bias=False, dtype=tf.float32, name="dense")
    return output_layer


def lstm_cell_w_dropout(hsz, pkeep):
    return tf.contrib.rnn.DropoutWrapper(tf.contrib.rnn.BasicLSTMCell(hsz, forget_bias=0.0, state_is_tuple=True), output_keep_prob=pkeep)


def stacked_lstm(hsz, pkeep, nlayers):
    return tf.contrib.rnn.MultiRNNCell([lstm_cell_w_dropout(hsz, pkeep) for _ in range(nlayers)], state_is_tuple=True)

def new_rnn_cell(hsz, rnntype, st=None):
    if st is not None:
        return tf.contrib.rnn.BasicLSTMCell(hsz, state_is_tuple=st) if rnntype == 'lstm' else tf.contrib.rnn.GRUCell(hsz)
    return tf.contrib.rnn.LSTMCell(hsz) if rnntype == 'lstm' else tf.contrib.rnn.GRUCell(hsz)


def new_multi_rnn_cell(hsz, name, num_layers):
    return tf.contrib.rnn.MultiRNNCell([new_rnn_cell(hsz, name) for _ in range(num_layers)], state_is_tuple=True)


def show_examples_tf(model, es, rlut1, rlut2, embed2, mxlen, sample, prob_clip, max_examples, reverse):
    si = np.random.randint(0, len(es))

    src_array, tgt_array, src_len, _ = es[si]

    if max_examples > 0:
        max_examples = min(max_examples, src_array.shape[0])
        src_array = src_array[0:max_examples]
        tgt_array = tgt_array[0:max_examples]
        src_len = src_len[0:max_examples]

    GO = embed2.vocab['<GO>']
    EOS = embed2.vocab['<EOS>']

    for src_len_i,src_i,tgt_i in zip(src_len, src_array, tgt_array):

        print('========================================================================')

        sent = lookup_sentence(rlut1, src_i, reverse=reverse)
        print('[OP] %s' % sent)
        sent = lookup_sentence(rlut2, tgt_i)
        print('[Actual] %s' % sent)
        dst_i = np.zeros((1, mxlen))
        src_i = src_i[np.newaxis,:]
        src_len_i = np.array([src_len_i])
        next_value = GO
        for j in range(mxlen):
            dst_i[0, j] = next_value
            tgt_len_i = np.array([j+1])
            output = model.step(src_i, src_len_i, dst_i, tgt_len_i)[j]
            if sample is False:
                next_value = np.argmax(output)
            else:
                # This is going to zero out low prob. events so they are not
                # sampled from
                next_value = beam_multinomial(prob_clip, output)

            if next_value == EOS:
                break

        sent = lookup_sentence(rlut2, dst_i.squeeze())
        print('Guess: %s' % sent)
        print('------------------------------------------------------------------------')


def skip_conns(inputs, wsz_all, n):
    for i in range(n):
        with tf.variable_scope("skip-%d" % i):
            W_p = tf.get_variable("W_p", [wsz_all, wsz_all])
            b_p = tf.get_variable("B_p", [1, wsz_all], initializer=tf.constant_initializer(0.0))
            proj = tf.nn.relu(tf.matmul(inputs, W_p) + b_p, "relu")

        inputs = inputs + proj
    return inputs


def highway_conns(inputs, wsz_all, n):
    for i in range(n):
        with tf.variable_scope("highway-%d" % i):
            W_p = tf.get_variable("W_p", [wsz_all, wsz_all])
            b_p = tf.get_variable("B_p", [1, wsz_all], initializer=tf.constant_initializer(0.0))
            proj = tf.nn.relu(tf.matmul(inputs, W_p) + b_p, "relu-proj")

            W_t = tf.get_variable("W_t", [wsz_all, wsz_all])
            b_t = tf.get_variable("B_t", [1, wsz_all], initializer=tf.constant_initializer(-2.0))
            transform = tf.nn.sigmoid(tf.matmul(inputs, W_t) + b_t, "sigmoid-transform")

        inputs = tf.multiply(transform, proj) + tf.multiply(inputs, 1 - transform)
    return inputs


def char_word_conv_embeddings(char_vec, filtsz, char_dsz, wsz):

    expanded = tf.expand_dims(char_vec, -1)
    mots = []
    for i, fsz in enumerate(filtsz):
        with tf.variable_scope('cmot-%s' % fsz):

            kernel_shape = [fsz, char_dsz, 1, wsz]

            # Weight tying
            W = tf.get_variable("W", kernel_shape)
            b = tf.get_variable("b", [wsz], initializer=tf.constant_initializer(0.0))

            conv = tf.nn.conv2d(expanded,
                                W, strides=[1,1,1,1],
                                padding="VALID", name="conv")

            activation = tf.nn.relu(tf.nn.bias_add(conv, b), "activation")

            mot = tf.reduce_max(activation, [1], keep_dims=True)
            # Add back in the dropout
            mots.append(mot)

    wsz_all = wsz * len(mots)
    combine = tf.reshape(tf.concat(values=mots, axis=3), [-1, wsz_all])

    # joined = highway_conns(combine, wsz_all, 1)
    joined = skip_conns(combine, wsz_all, 1)
    return joined


def shared_char_word(Wch, xch_i, filtsz, char_dsz, wsz, reuse):

    with tf.variable_scope("SharedCharWord", reuse=reuse):
        # Zeropad the letters out to half the max filter size, to account for
        # wide convolution.  This way we don't have to explicitly pad the
        # data upfront, which means our Y sequences can be assumed not to
        # start with zeros
        mxfiltsz = np.max(filtsz)
        halffiltsz = int(math.floor(mxfiltsz / 2))
        zeropad = tf.pad(xch_i, [[0,0], [halffiltsz, halffiltsz]], "CONSTANT")
        cembed = tf.nn.embedding_lookup(Wch, zeropad)
        if len(filtsz) == 0 or filtsz[0] == 0:
            return tf.reduce_sum(cembed, [1])
        return char_word_conv_embeddings(cembed, filtsz, char_dsz, wsz)
