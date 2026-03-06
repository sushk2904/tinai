import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
    scenarios: {
        constant_traffic: {
            executor: 'constant-arrival-rate',
            rate: 20,
            timeUnit: '1s', // 20 RPS
            duration: '10m', // 10 minutes total
            preAllocatedVUs: 20,
            maxVUs: 150,
        },
    },
};

const POLICIES = ["latency-first", "cost-first", "sla-aware"];
const PROMPTS = [
    "What is the capital of France? One word answer.",
    "Explain quantum computing in one short sentence.",
    "Who won the FIFA World Cup in 2022?",
    "Convert 100 USD to EUR based on historical averages.",
    "Write a haiku about artificial intelligence."
];

export default function () {
    const url = 'http://api:8000/v1/infer';

    const randomPolicy = POLICIES[Math.floor(Math.random() * POLICIES.length)];
    const randomPrompt = PROMPTS[Math.floor(Math.random() * PROMPTS.length)];

    let finalPrompt = randomPrompt;

    // 80% chance to add a cache-buster salt (Forces LLM hit)
    // 20% chance to leave it untouched (Forces L1 Cache hit)
    if (Math.random() < 0.80) {
        finalPrompt = `${randomPrompt} (Ref: ${Date.now()}-${Math.random().toString(36).substring(7)})`;
    }

    const payload = JSON.stringify({
        prompt: finalPrompt,
        policy: randomPolicy
    });

    const params = {
        headers: {
            'Content-Type': 'application/json',
            'x-api-key': '5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE',
        },
        timeout: '15s',
    };

    const res = http.post(url, payload, params);

    check(res, {
        'is status 200': (r) => r.status === 200,
        'is status 503 (shedded/failed)': (r) => r.status === 503,
    });
}