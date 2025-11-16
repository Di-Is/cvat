// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

export function isCrossOriginIsolated(): boolean {
    try {
        return Boolean(window.crossOriginIsolated);
    } catch {
        return false;
    }
}
