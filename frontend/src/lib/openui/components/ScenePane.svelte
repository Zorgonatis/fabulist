<script lang="ts">
	// OpenUI root component: a titled panel that hosts the scene children.
	// `renderNode` is the renderer-provided snippet for `props.children`.
	//
	// D48: the root re-emits `root = ScenePane(...)` after every panel-bearing
	// turn, so the placard reads as a quiet title line above the panel group
	// rather than a boxed accent header repeating over the prose.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';

	let { props, renderNode }: ComponentRenderProps<{ title: string; children?: unknown }> = $props();
</script>

<section class="scene-pane" data-openui="ScenePane">
	<header class="scene-title">{props.title}</header>
	<div class="scene-body">
		{@render renderNode(props.children)}
	</div>
</section>

<style>
	.scene-pane {
		display: flex;
		flex-direction: column;
		gap: 0.55rem;
	}
	.scene-title {
		font-size: 0.72rem;
		font-weight: 500;
		letter-spacing: 0.02em;
		color: var(--text-muted);
	}
	.scene-body {
		display: flex;
		flex-direction: column;
		gap: 0.55rem;
	}
</style>
