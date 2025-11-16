import { defineConfig } from 'vitest/config';
import path from 'path';

export default defineConfig({
    test: {
        environment: 'jsdom',
        include: ['tests/**/*.spec.ts'],
        globals: true,
        setupFiles: [path.resolve(__dirname, 'tests/setup.ts')],
    },
    resolve: {
        alias: {
            components: path.resolve(__dirname, 'src/components'),
            utils: path.resolve(__dirname, 'src/utils'),
            'cvat-core-wrapper': path.resolve(__dirname, 'src/cvat-core-wrapper.ts'),
        },
    },
});
