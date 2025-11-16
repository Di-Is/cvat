// Copyright (C) 2020-2022 Intel Corporation
// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

// Safari < 15 does not implement Promise.allSettled. Provide a minimal polyfill
// so model loading keeps working on such browsers.

declare global {
    interface PromiseConstructor {
        allSettled<T>(iterable: Iterable<T | PromiseLike<T>>): Promise<PromiseSettledResult<T>[]>;
    }
}

if (typeof Promise.allSettled !== 'function') {
    Promise.allSettled = function allSettled<T>(iterable: Iterable<T | PromiseLike<T>>): Promise<PromiseSettledResult<T>[]> {
        const wrapped = Array.from(iterable).map((item) => Promise.resolve(item)
            .then<PromiseSettledResult<T>>((value) => ({
                status: 'fulfilled',
                value,
            }))
            .catch<PromiseSettledResult<T>>((reason) => ({
                status: 'rejected',
                reason,
            })));

        return Promise.all(wrapped);
    };
}

export {};
