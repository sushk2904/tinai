import http from 'k6/http';
import { check } from 'k6';

export const options = {
    scenarios: {
        thirty_seven_min_soak: {
            executor: 'ramping-arrival-rate',
            startRate: 10,
            timeUnit: '1s',
            preAllocatedVUs: 50,
            maxVUs: 150,
            stages: [
                { target: 40, duration: '5m' },   // Phase 1: Ramp up aggressively to 40 RPS
                { target: 40, duration: '25m' },  // Phase 2: Hold peak saturation for 25 minutes
                { target: 0, duration: '7m' },    // Phase 3: Smooth ramp down to 0
            ],
        },
    },
    thresholds: {
        http_req_failed: ['rate<0.95'],
    },
};

const POLICIES = ["latency-first", "cost-first", "sla-aware", "quality-first"];

function generateDynamicPrompt() {
    const actions = [
        "Explain", "Write a short python snippet for", "Give me 3 bullet points on",
        "Translate a sentence into French about", "What is the historical significance of",
        "Design a database schema for", "Write an algorithm to calculate"
    ];

    const topics = [
        "scalable ML inference systems",
        "the offside rule in modern football",
        "simulating autonomous civilization growth",
        "DNA replication and sequencing",
        "handling 1 million requests per day",
        "dynamic birth and death rate graphs",
        "multi-armed bandit routing policies"
    ];

    const constraints = [
        "under 50 words.", "using simple terms.", "in the style of a technical mentor.",
        "with exactly one analogy.", "and output only JSON."
    ];

    const randomAction = actions[Math.floor(Math.random() * actions.length)];
    const randomTopic = topics[Math.floor(Math.random() * topics.length)];
    const randomConstraint = constraints[Math.floor(Math.random() * constraints.length)];

    const traceId = `TX-${Math.floor(Math.random() * 9999999)}`;

    return `${randomAction} ${randomTopic} ${randomConstraint} [Trace: ${traceId}]`;
}

export default function () {
    const url = 'http://api:8000/v1/infer';

    const randomPolicy = POLICIES[Math.floor(Math.random() * POLICIES.length)];

    let finalPrompt = "What is the capital of France? One word.";
    if (Math.random() < 0.80) {
        finalPrompt = generateDynamicPrompt();
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
        timeout: '20s',
    };

    const res = http.post(url, payload, params);

    check(res, {
        'is status 200': (r) => r.status === 200,
        'is status 503': (r) => r.status === 503,
        'is status 429': (r) => r.status === 429,
    });
}