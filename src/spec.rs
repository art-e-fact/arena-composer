//! The spec as authored, and the vocabulary it can be authored from.
//!
//! [`crate::scene`] carries the layout a solve produced — where things ended
//! up. This carries what was asked for, which is the half an editor can write
//! back. The two arrive in the same payload because the panel shows one and
//! the viewport the other.
//!
//! Nothing here knows what a relation kind is called or what parameters it
//! takes. That is inferred from Arena's registries on the Python side
//! (`arena_composer.catalog`) and arrives as [`Vocabulary`], so a relation
//! somebody registers themselves gets a form without touching this file.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// The task and the scene it includes, merged as Arena merges them.
#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct Spec {
    pub env_name: String,
    pub embodiment: Node,
    pub background: Node,
    #[serde(deserialize_with = "null_is_empty")]
    pub objects: Vec<Node>,
    #[serde(deserialize_with = "null_is_empty")]
    pub object_sets: Vec<ObjectSet>,
    #[serde(deserialize_with = "null_is_empty")]
    pub object_references: Vec<ObjectReference>,
    #[serde(deserialize_with = "null_is_empty")]
    pub relations: Vec<Relation>,
    pub task: Task,
}

impl Spec {
    /// Every id a relation or task parameter may name, in declaration order.
    ///
    /// This is what a node dropdown offers, and offering exactly this is what
    /// keeps a dangling reference out of the spec — Arena rejects one, so the
    /// form is built not to express it.
    pub fn node_ids(&self) -> Vec<&str> {
        let mut ids = vec![self.embodiment.id.as_str(), self.background.id.as_str()];
        ids.extend(self.objects.iter().map(|node| node.id.as_str()));
        ids.extend(self.object_sets.iter().map(|set| set.id.as_str()));
        ids.extend(self.object_references.iter().map(|node| node.id.as_str()));
        ids.retain(|id| !id.is_empty());
        ids
    }
}

/// An embodiment, a background or an object: an id and the asset behind it.
#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct Node {
    pub id: String,
    pub registry_name: String,
    pub params: Params,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct ObjectSet {
    pub id: String,
    pub members: Vec<String>,
    pub random_choice: bool,
    pub params: Params,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct ObjectReference {
    pub id: String,
    pub parent_id: String,
    pub prim_path: Option<String>,
    /// `base`, `rigid` or `articulation`.
    pub object_type: String,
    pub params: Params,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct Relation {
    pub kind: String,
    pub subject: String,
    /// Absent on a unary relation, and required on every other — the kind
    /// decides, so [`Kind::unary`] is what says whether to draw the slot.
    pub reference: Option<String>,
    pub params: Params,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct Task {
    /// `atomic`, `sequential` or `parallel`.
    pub composition: String,
    pub description: String,
    pub subtasks: Vec<Subtask>,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct Subtask {
    pub kind: String,
    pub params: Params,
}

/// A `params` mapping. Kept as loose values rather than typed per kind: what
/// the keys mean comes from [`Vocabulary`] at runtime, and a key no form shows
/// still has to survive being written back.
pub type Params = HashMap<String, Value>;

/// A parameter value, in the shapes a spec's YAML can hold one.
#[derive(Debug, Clone, PartialEq, Default, Deserialize, Serialize)]
#[serde(untagged)]
pub enum Value {
    #[default]
    Null,
    Bool(bool),
    Number(f64),
    Text(String),
    List(Vec<Value>),
    /// A whole entry, which is what an insert carries — a new object or
    /// relation is a mapping, not a leaf.
    Map(HashMap<String, Value>),
}

impl Value {
    /// A mapping built from pairs, for the entry an insert adds.
    pub fn map<'a>(pairs: impl IntoIterator<Item = (&'a str, Value)>) -> Self {
        Value::Map(pairs.into_iter().map(|(key, value)| (key.to_owned(), value)).collect())
    }

    pub fn text(value: impl Into<String>) -> Self {
        Value::Text(value.into())
    }
}

impl Value {
    pub fn as_number(&self) -> Option<f64> {
        match self {
            Value::Number(value) => Some(*value),
            _ => None,
        }
    }

    pub fn as_text(&self) -> Option<&str> {
        match self {
            Value::Text(value) => Some(value),
            _ => None,
        }
    }
}

/// Everything a spec may name: the registered assets, and the relation and
/// task kinds with the form that edits each.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Vocabulary {
    pub assets: Vec<CatalogAsset>,
    pub relations: Vec<Kind>,
    pub tasks: Vec<Kind>,
    /// How a task's subtasks combine: `atomic`, `sequential` or `parallel`.
    pub compositions: Vec<String>,
}

impl Vocabulary {
    pub fn relation(&self, kind: &str) -> Option<&Kind> {
        self.relations.iter().find(|entry| entry.name == kind)
    }

    pub fn task(&self, kind: &str) -> Option<&Kind> {
        self.tasks.iter().find(|entry| entry.name == kind)
    }
}

/// One asset a spec can name: registered with Arena, or a SimReady prop the
/// local index knows. The two differ only in what an entry naming one has to
/// carry, which is [`CatalogAsset::params`], so the browser lists them together.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct CatalogAsset {
    pub registry_name: String,
    /// `embodiment`, `background` or `object`.
    pub role: String,
    pub tags: Vec<String>,
    pub object_type: Option<String>,
    /// What to call it. The registry name for a registered asset, and the
    /// prop's own folder name for a SimReady one — every one of those shares
    /// the single registry name `simready_usd_object`.
    pub name: String,
    /// What an entry naming it needs in `params` beyond the registry name:
    /// empty for a registered asset, the URL for a SimReady prop. Written
    /// through as given, so picking one needs no knowledge of which kind it is.
    pub params: Params,
    /// The USD it spawns from, whether or not `params` also carries it. It is
    /// what a row is searched and hovered by, and empty for an asset that
    /// composes its USD rather than naming a published one.
    pub usd_path: String,
    /// The asset's picture, as the file Python mirrored it to. Python fetches
    /// every one it can before it sends a list, so a row either has its picture
    /// or there is none to have: this is empty where the bucket publishes
    /// none beside the layer.
    pub thumbnail: String,
    /// A SimReady prop's folders under the bucket's SimReady root, e.g.
    /// `Residential/Kitchen/Baskets`: where the browser's tree files it.
    /// Empty for a registered asset.
    pub category: String,
}

impl CatalogAsset {
    /// Whether `node` is an entry naming this asset: the same registry name,
    /// carrying the params that pick this asset out. That is all of it for a
    /// registered asset, and the URL as well for a prop, since every prop
    /// shares one registry name.
    pub fn is_named_by(&self, node: &Node) -> bool {
        self.registry_name == node.registry_name
            && self.params.iter().all(|(key, value)| node.params.get(key) == Some(value))
    }
}

/// A registered relation or task kind, and the form that edits it.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Kind {
    pub name: String,
    pub fields: Vec<Field>,
    pub summary: String,
    /// Relations only: a unary relation takes no `reference`.
    pub unary: Option<bool>,
}

/// One parameter of a kind, as the control that edits it.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Field {
    pub name: String,
    /// `number`, `int`, `bool`, `text`, `enum` or `node_ref`. A string rather
    /// than an enum so a kind added on the Python side draws *something*
    /// instead of failing the whole payload to decode.
    pub kind: String,
    /// Accepts null, so the control needs a way to say "unset" — a number box
    /// on its own cannot.
    pub optional: bool,
    /// No default, so the spec is invalid until this is given one.
    pub required: bool,
    pub default: Value,
    /// The values of an `enum`, empty for every other kind.
    pub options: Vec<String>,
}

/// What the editor panel asks Python to do.
///
/// The first three edit the spec. A `Set` names one leaf wherever it can, so a
/// parameter the form does not show is left where it is. The exception is a
/// relation's kind: the old kind's parameters mean nothing to the new one, and
/// whether the entry has a `reference` at all is the kind's to say, so that one
/// replaces the entry.
///
/// Python applies an edit through `arena_composer.spec_edit`, which owns which
/// file the key lives in and validates before either file is written — so a
/// rejected edit comes back as `errors` on the next scene with the spec
/// untouched.
///
/// The last asks for assets instead, and is answered by a message carrying
/// [`Scene::remote`](crate::scene::Scene::remote). It is network, not a write,
/// so `arena_composer.view` serves it beside the edits rather than in front of
/// them.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Request {
    Set { path: Vec<Step>, value: Value },
    Insert { path: Vec<Step>, value: Value },
    Remove { path: Vec<Step> },
    /// Crawl NVIDIA's bucket again and write the index the prop list comes
    /// from, then fetch every prop's picture. Minutes of network; no prop
    /// itself is downloaded.
    Reindex,
}

/// One step into the merged spec: `["relations", 2, "params", "side"]`.
///
/// Hashable, so a whole path can name the widget that edits it: every leaf is
/// drawn once, and its path is what a half-made edit is kept against.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
#[serde(untagged)]
pub enum Step {
    Key(String),
    Index(usize),
}

impl From<&str> for Step {
    fn from(key: &str) -> Self {
        Step::Key(key.to_owned())
    }
}

impl From<usize> for Step {
    fn from(index: usize) -> Self {
        Step::Index(index)
    }
}

/// `null` where a list is expected.
///
/// Arena writes an absent list as `null` rather than leaving the key out, and
/// `#[serde(default)]` only covers a key that is missing entirely.
fn null_is_empty<'de, D, T>(deserializer: D) -> Result<Vec<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    Ok(Option::<Vec<T>>::deserialize(deserializer)?.unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// An absent list reaches us as `null`, not as a missing key, so
    /// `#[serde(default)]` alone would fail the whole spec to decode.
    #[test]
    fn a_null_list_decodes_as_an_empty_one() {
        let packed = rmp_serde::to_vec_named(&serde_json::json!({
            "env_name": "demo",
            "object_sets": null,
            "relations": [{"kind": "on", "subject": "bowl", "reference": "table", "params": {}}],
        }))
        .unwrap();

        let spec: Spec = rmp_serde::from_slice(&packed).unwrap();
        assert_eq!(spec.env_name, "demo");
        assert!(spec.object_sets.is_empty());
        assert_eq!(spec.relations[0].reference.as_deref(), Some("table"));
    }

    /// A unary relation has no reference, and its params carry mixed types.
    #[test]
    fn params_keep_the_shapes_yaml_gave_them() {
        let packed = rmp_serde::to_vec_named(&serde_json::json!({
            "relations": [{
                "kind": "position_limits_box",
                "subject": "cube",
                "reference": null,
                "params": {"x_min": 0.55, "z_min": null, "side": "positive_x"},
            }],
        }))
        .unwrap();

        let spec: Spec = rmp_serde::from_slice(&packed).unwrap();
        let params = &spec.relations[0].params;
        assert!(spec.relations[0].reference.is_none());
        assert_eq!(params["x_min"].as_number(), Some(0.55));
        assert_eq!(params["side"].as_text(), Some("positive_x"));
        assert_eq!(params["z_min"], Value::Null);
    }

    /// The fixtures above are hand-written; this is the shape Python actually
    /// packs. Ignored because it needs a payload to read. Make one first:
    /// `arena-resolve-layout <spec>.yaml --payload scene.msgpack`, then
    /// `ARENA_COMPOSER_PAYLOAD=scene.msgpack cargo test -- --ignored`.
    #[test]
    #[ignore = "needs a payload from arena-resolve-layout --payload"]
    fn a_real_payload_carries_a_form_for_every_relation_it_uses() {
        let path =
            std::env::var("ARENA_COMPOSER_PAYLOAD").unwrap_or_else(|_| "scene.msgpack".to_string());
        let bytes = std::fs::read(&path)
            .unwrap_or_else(|e| panic!("{path}: {e} — run arena-resolve-layout --payload first"));
        let scene = crate::scene::Scene::from_msgpack(&bytes).expect("decoding the payload");

        assert!(!scene.spec.env_name.is_empty(), "the authored spec is missing");
        assert!(!scene.vocabulary.assets.is_empty(), "the asset catalogue is missing");

        let ids = scene.spec.node_ids();
        for relation in &scene.spec.relations {
            let kind = scene
                .vocabulary
                .relation(&relation.kind)
                .unwrap_or_else(|| panic!("no vocabulary for relation kind {:?}", relation.kind));
            // Arity decides whether the form draws a reference slot at all, so
            // a kind that disagrees with the spec would draw the wrong control.
            assert_eq!(
                kind.unary,
                Some(relation.reference.is_none()),
                "{:?} arity disagrees with the spec",
                relation.kind
            );
            assert!(ids.contains(&relation.subject.as_str()), "{:?} names an unknown subject", relation.kind);
        }
    }

    /// An entry names a prop by its URL as well as the registry name every
    /// prop shares, and a registered asset by the registry name alone.
    #[test]
    fn an_entry_names_the_asset_its_params_pick_out() {
        let url = |name: &str| Value::text(format!("https://bucket/{name}.usd"));
        let prop = |name: &str| CatalogAsset {
            registry_name: "simready_usd_object".into(),
            params: Params::from([("usd_path".to_owned(), url(name))]),
            ..Default::default()
        };
        let node = Node {
            id: "basket".into(),
            registry_name: "simready_usd_object".into(),
            params: Params::from([("usd_path".to_owned(), url("basket")), ("scale".to_owned(), Value::Number(2.0))]),
        };
        assert!(prop("basket").is_named_by(&node), "extra params on the entry hide its asset");
        assert!(!prop("bucket").is_named_by(&node), "another prop's entry");

        let banana = CatalogAsset { registry_name: "banana_ycb_robolab".into(), ..Default::default() };
        let named = Node { registry_name: "banana_ycb_robolab".into(), ..Default::default() };
        assert!(banana.is_named_by(&named));
        assert!(!banana.is_named_by(&node));
    }

    /// The dropdowns offer exactly the ids Arena will accept, in the order the
    /// spec declares them.
    #[test]
    fn node_ids_span_every_namespace_a_relation_can_name() {
        let packed = rmp_serde::to_vec_named(&serde_json::json!({
            "embodiment": {"id": "droid", "registry_name": "droid_abs_joint_pos"},
            "background": {"id": "table", "registry_name": "maple_table_robolab"},
            "objects": [{"id": "bowl", "registry_name": "bowl_ycb_robolab"}],
            "object_sets": [{"id": "snacks", "members": ["jug"]}],
            "object_references": [{"id": "table_top", "parent_id": "table", "object_type": "rigid"}],
        }))
        .unwrap();

        let spec: Spec = rmp_serde::from_slice(&packed).unwrap();
        assert_eq!(spec.node_ids(), ["droid", "table", "bowl", "snacks", "table_top"]);
    }
}
