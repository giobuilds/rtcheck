/* The dispatch case: an effect chain of function pointers.
 *
 * process_chain() calls whatever is in the table. A tool that only follows
 * direct calls will declare this clean, because it cannot see the callees.
 * rtcheck says so out loud in the default mode, and in --indirect=address-taken
 * it assumes any function whose address is taken might be the target.
 */
#include <stdlib.h>

typedef float (*EffectFn)(float sample, void *state);

typedef struct {
    EffectFn fn;
    void    *state;
} Effect;

typedef struct {
    Effect slots[8];
    int    count;
} Chain;

/* Safe: pure arithmetic. */
static float fx_gain(float sample, void *state)
{
    return sample * (*(float *)state);
}

/* Not safe: grows a scratch buffer on demand. */
static float fx_chorus(float sample, void *state)
{
    float *scratch = realloc(state, 4096);
    return sample + scratch[0] * 0.5f;
}

float process_chain(Chain *c, float sample)
{
    for (int i = 0; i < c->count; i++)
        sample = c->slots[i].fn(sample, c->slots[i].state);
    return sample;
}

void chain_setup(Chain *c)
{
    c->slots[0].fn = fx_gain;
    c->slots[1].fn = fx_chorus;
    c->count = 2;
}
