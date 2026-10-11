import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-xl flex-col items-center justify-center gap-3 px-6 text-center">
      <p className="text-sm font-semibold text-brand">404</p>
      <h1 className="text-2xl font-semibold tracking-tight">This page isn&apos;t available</h1>
      <p className="text-muted">
        The career site or role you&apos;re looking for may have been filled or moved.
      </p>
      <Link href="/" className="mt-2 text-sm font-medium text-brand underline-offset-2 hover:underline">
        Go home
      </Link>
    </main>
  );
}
