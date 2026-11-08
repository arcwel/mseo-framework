"""Article shapes: the order of blocks in a post. Rotating the order keeps posts from reading like one template.
Every shape keeps the AEO core: bolded short answer first, question-style H2s, FAQ, author, CTA, sources, NAP footer.

The shapes and axes live in templates/posts/recipes.json (read by content/recipes.py); this module keeps the names
older callers use (SHAPES, INTROS, LISTS, FAQ_N, CTA_POS, TITLES, distance, recipe)."""

from mseo.content import recipes as recipes_mod
from mseo.content.recipes import distance  # noqa: F401 - re-exported for older callers

SHAPES = recipes_mod.shapes()
INTROS = recipes_mod.axis_values("article", "intro")
LISTS = ["numbered", "bullets", "table"]
FAQ_N = recipes_mod.axis_values("article", "faq_n")


def recipe(log_path, allowed=None):
    """The next article's recipe (shape and every axis), most different from the logged articles. `log_path` is the
    workspace's content log (Workspace.file("content_log")): there is no cwd-relative default; the window comes from
    recipes.json."""
    return recipes_mod.recipe(log_path, "article", allowed=allowed)
