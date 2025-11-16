class WorkerStub {
    onmessage: ((event: MessageEvent) => void) | null = null;

    // eslint-disable-next-line class-methods-use-this
    postMessage(): void {}

    // eslint-disable-next-line class-methods-use-this
    terminate(): void {}
}

if (!(globalThis as any).Worker) {
    (globalThis as any).Worker = WorkerStub;
}
