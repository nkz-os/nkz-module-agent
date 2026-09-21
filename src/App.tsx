/**
 * Main page component for this module — lazily loaded as `MainPage` by
 * moduleEntry.ts's `defineModule({ main: MainPage })`. Part of the
 * production bundle (not a dev-only shell); also rendered standalone by
 * `npm run dev` via src/main.tsx.
 *
 * Replace the body with your real UI.
 */
import React from 'react';
import './index.css';

const App: React.FC = () => {
  return (
    <div className="w-full min-h-screen bg-gray-50 flex items-center justify-center">
      <div className="bg-white rounded-lg shadow p-8 max-w-md w-full">
        <h1 className="text-2xl font-bold text-gray-900 mb-2">Agent</h1>
        <p className="text-sm text-gray-500 mb-4">
          Replace this page with your module's main UI.
        </p>
        <div className="bg-blue-50 border border-blue-200 rounded p-3 text-sm text-blue-800">
          Run <code className="font-mono bg-blue-100 px-1 rounded">npm run build:module</code> to
          produce <code className="font-mono bg-blue-100 px-1 rounded">dist/remoteEntry.js</code> +{' '}
          <code className="font-mono bg-blue-100 px-1 rounded">dist/mf-manifest.json</code>, then
          upload the whole <code className="font-mono bg-blue-100 px-1 rounded">dist/</code> directory
          to MinIO.
        </div>
      </div>
    </div>
  );
};

export default App;
