<script lang="ts">
	// Circular identity badge (owner request): a small round avatar used to the
	// LEFT of each user message (the active persona) and to the RIGHT of the DSL
	// header title (the active world/cast). With no stored image it degrades to
	// the name's initials on a colour derived from the name, so a definition
	// without art still reads as an identity chip.
	//
	// The definition image route is keyed by the DEFINITION id, not by the image
	// asset id: `GET /api/personas/{persona_id}/image` /
	// `GET /api/worlds/{world_id}/image` (T4a). So the caller passes BOTH `id`
	// (the persona/world id the route is keyed by) and `imageAsset` (the wire
	// `image_asset` content address, which alone says an image exists) — the
	// image renders only when both are present; otherwise the initials show.
	import { API_BASE } from '#lib/api';

	let {
		name,
		id = null,
		imageAsset = null,
		kind,
		size = '1.5rem'
	}: {
		/** Display name; also the accessible label for the badge. */
		name: string;
		/** The definition id (persona_id / world_id) the image route is keyed by. */
		id?: string | null;
		/** The definition's `image_asset`; its presence gates the image render. */
		imageAsset?: string | null;
		/** 'cast' members are personas, so they resolve through the persona route. */
		kind: 'persona' | 'world' | 'cast';
		/** Rendered diameter (any CSS length). */
		size?: string;
	} = $props();

	/** The definition image URL, or null when there is no image / no id. */
	const src = $derived(
		id && imageAsset
			? `${API_BASE}/api/${kind === 'world' ? 'worlds' : 'personas'}/${encodeURIComponent(id)}/image`
			: null
	);

	// Falls back to the initials when the image 404s or fails to decode.
	let imageFailed = $state(false);
	$effect(() => {
		// Reset the fallback whenever the resolved source changes (a different
		// definition/asset). Reading `src` registers the dependency.
		void src;
		imageFailed = false;
	});

	/** 1–2 letters: first two of a one-word name, else the two leading initials. */
	function initialsOf(value: string): string {
		const words = value.trim().split(/\s+/).filter(Boolean);
		if (words.length === 0) return '?';
		if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
		return `${words[0][0] ?? ''}${words[1][0] ?? ''}`.toUpperCase();
	}
	const initials = $derived(initialsOf(name));

	/** Deterministic hue from the name so distinct identities get distinct chips. */
	function hashHue(value: string): number {
		let acc = 0;
		for (let i = 0; i < value.length; i += 1) acc = (acc * 31 + value.charCodeAt(i)) % 360;
		return acc;
	}
	const hue = $derived(hashHue(name));
</script>

<span
	class="user-badge"
	style={`--badge-size: ${size}; --badge-hue: ${hue};`}
	role="img"
	aria-label={name}
	title={name}
>
	{#if src && !imageFailed}
		<img class="badge-image" {src} alt="" onerror={() => (imageFailed = true)} />
	{:else}
		<span class="badge-initials" aria-hidden="true">{initials}</span>
	{/if}
</span>

<style>
	.user-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex: 0 0 auto;
		width: var(--badge-size);
		height: var(--badge-size);
		border-radius: 50%;
		overflow: hidden;
		/* Theme-derived: a stable hue from the name keeps each identity distinct
		   while the saturation/lightness stay legible against the dark shell. */
		background: hsl(var(--badge-hue) 42% 42%);
		color: #fff;
		font-size: calc(var(--badge-size) * 0.42);
		font-weight: 600;
		line-height: 1;
		letter-spacing: 0.02em;
		user-select: none;
		box-shadow: inset 0 0 0 1px rgb(255 255 255 / 12%);
	}
	.badge-image {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.badge-initials {
		color: inherit;
	}
</style>
