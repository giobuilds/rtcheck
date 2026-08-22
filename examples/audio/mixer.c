/* A small, deliberately flawed audio engine.
 *
 * mix_frame() runs on the audio thread. It must never allocate, lock, or do
 * I/O. Reading it, everything looks fine. The problem is four calls away.
 */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <pthread.h>

#define MAX_TAPS 512

typedef struct {
    float *taps;
    int    n_taps;
    int    capacity;
} Delay;

typedef struct {
    Delay delay;
    float wet;
} Reverb;

static Reverb g_reverb;
static pthread_mutex_t g_param_lock = PTHREAD_MUTEX_INITIALIZER;

/* --- the buried problem ------------------------------------------------ */

static void ensure_capacity(Delay *d, int needed)
{
    if (needed <= d->capacity)
        return;
    /* Looks harmless. Runs on the audio thread. Allocates. */
    d->taps = realloc(d->taps, (size_t)needed * sizeof(float));
    d->capacity = needed;
}

static void push_tap(Delay *d, float value)
{
    ensure_capacity(d, d->n_taps + 1);
    d->taps[d->n_taps++] = value;
}

/* --- reverb ------------------------------------------------------------ */

static float apply_reverb(Reverb *r, float sample)
{
    push_tap(&r->delay, sample);
    float sum = 0.0f;
    for (int i = 0; i < r->delay.n_taps && i < MAX_TAPS; i++)
        sum += r->delay.taps[i];
    return sample + r->wet * (sum / (float)MAX_TAPS);
}

/* --- a second, different violation: logging on the hot path ------------ */

static void trace_clip(float peak)
{
    printf("clipping at %f\n", peak);
}

static float clamp(float x)
{
    if (x > 1.0f) {
        trace_clip(x);
        return 1.0f;
    }
    return x < -1.0f ? -1.0f : x;
}

/* --- a third: taking a lock to read a parameter ------------------------ */

static float current_wet(void)
{
    pthread_mutex_lock(&g_param_lock);
    float w = g_reverb.wet;
    pthread_mutex_unlock(&g_param_lock);
    return w;
}

/* --- the real-time entry point ----------------------------------------- */

void mix_frame(float *out, const float *in, int frames)
{
    g_reverb.wet = current_wet();
    for (int i = 0; i < frames; i++)
        out[i] = clamp(apply_reverb(&g_reverb, in[i]));
}

/* --- setup, which is allowed to do all of the above -------------------- */

void engine_init(int capacity)
{
    g_reverb.delay.taps = calloc((size_t)capacity, sizeof(float));
    g_reverb.delay.capacity = capacity;
    g_reverb.wet = 0.25f;
    printf("engine ready\n");
}
