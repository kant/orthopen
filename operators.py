import copy
import math
from pathlib import Path

import bmesh
import bpy
import bpy_extras
import mathutils
import numpy as np

from . import helpers

# If a bpy.types.Object contains this key, we know it is a scan we imported
_KEY_IMPORTED_SCAN = "imported_3d_scan"

# Key to identify armature managed by this add-on
_KEY_MANAGED_ARMATURE = "managed_armature"


def _clear_managed_armature(object: bpy.types.Object):
    """
    Identify and remove managed (automatically generated) armature attached to object
    """

    for modifier in object.modifiers:
        try:
            # The armature modifier is tied to an armature object
            if _KEY_MANAGED_ARMATURE in modifier.object.keys():
                object.modifiers.remove(modifier)
        except AttributeError:
            pass

    # Now remove the armature itself
    if object.parent is not None:
        if _KEY_MANAGED_ARMATURE in object.parent.keys():
            bpy.data.objects.remove(object.parent, do_unlink=True)


class ORTHOPEN_OT_permanent_modifiers(bpy.types.Operator):
    """
    Permanently apply modifiers (e.g. changed foot angle) to the selected object. Will
    try to automtically find relevant objects if no object is selected.
    """
    bl_idname = helpers.mangle_operator_name(__qualname__)
    bl_label = "Apply changes"

    @classmethod
    def poll(cls, context):
        try:
            return bpy.context.object.mode != 'EDIT'
        except AttributeError:
            return False

    def execute(self, context):
        # This is an original object, without modifiers, or an object without a mesh such as a bone
        if context.active_object is None or context.active_object.type != 'MESH':
            objects_to_permanent = [o for o in bpy.data.objects if _KEY_IMPORTED_SCAN in o.keys()]

            if(len(objects_to_permanent) == 0):
                self.report({'INFO'}, "Could not find a relevant object to permanent")
                return {'CANCELLED'}
        else:
            objects_to_permanent = [context.active_object]

        # Apply all modifiers, such as ankle angle changed by bones
        # See: https://docs.blender.org/api/current/bpy.types.Depsgraph.html
        depedency_graph = bpy.context.evaluated_depsgraph_get()

        for object in objects_to_permanent:
            # Overwrite the old mesh with the mesh from modifiers
            object.data = bpy.data.meshes.new_from_object(object.evaluated_get(depedency_graph))

            # If we tagged the parent, is likely an foot adjustment armature that will not work after the
            # modifiers are cleared, and it will probably only be confusing to a user to keep it
            _clear_managed_armature(object)

            # The modifiers are already applied implicitly now, so keeping them would apply them twice
            object.modifiers.clear()

        context.collection.objects.update()

        self.report(
            {'INFO'},
            f"Permanently applied modifiers to '{','.join([o.name for o in objects_to_permanent])}'")

        return {'FINISHED'}


class ORTHOPEN_OT_set_foot_pivot(bpy.types.Operator):
    """
    When in edit mode, mark a vertex that will set a pivot point around which the foot angle can be adjusted.
    """
    bl_idname = helpers.mangle_operator_name(__qualname__)
    bl_label = "Set pivot point"

    # Used to identify anything auto-generated by name. For bpy.types.Object we can use a hidden property,
    # however we do not have that option for modifiers, vertexgroups etc
    _FOOT_AUTOGEN_ID = "foot_auto_gen"

    @classmethod
    def poll(cls, context):
        try:
            return bpy.context.active_object.data.total_vert_sel == 1
        except AttributeError:
            return False

    def execute(self, context):
        # We need to switch from 'Edit mode' to 'Object mode' so the selection gets updated
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='EDIT')

        leg = context.active_object

        # Identify the foot by a vertex group. First remove any
        # previously generated vertex groups
        for vertex_group in (leg.vertex_groups):
            if self._FOOT_AUTOGEN_ID in vertex_group.name:
                leg.vertex_groups.remove(vertex_group)
        foot = leg.vertex_groups.new(name=self._FOOT_AUTOGEN_ID)

        # The foot will be rotated around the ankle, which we have asked the user to mark
        selected_verts = [v.co for v in leg.data.vertices if v.select]
        assert len(selected_verts) == 1, "Only one vertex can be selected"
        ankle_point = copy.deepcopy(selected_verts[0])  # Deep copy here is important!

        self._weight_paint(foot, ankle_point)

        _clear_managed_armature(leg)
        armature = self._add_armature(ankle_point, foot.name)

        # Remove previous modifiers
        for modifier in leg.modifiers:
            if self._FOOT_AUTOGEN_ID in modifier.name:
                leg.modifiers.remove(modifier)

        # This might be the most important aspect for getting a realistic angle adjustment
        corrective_smooth = leg.modifiers.new(name=self._FOOT_AUTOGEN_ID, type="CORRECTIVE_SMOOTH")
        corrective_smooth.factor = 1
        corrective_smooth.iterations = 80

        # Select armature, this is probably what the user is interested in now
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        armature.select_set(True)

        # The user probably wants to adjust the foot angle now and that has to be done in pose mode
        bpy.ops.object.mode_set(mode='POSE')

        return {'FINISHED'}

    def _weight_paint(self, foot: bpy.types.VertexGroup, ankle_point: mathutils.Vector):
        """
        Add weight paint to the foot vertex group.
        The weight paint defines how the mesh will deform when coupled with an armature.
        """
        bpy.ops.object.mode_set(mode='OBJECT')

        # Set the default weight to zero
        foot.add(index=[v.index for v in bpy.context.active_object.data.vertices], weight=0, type='REPLACE')

        for vertex in bpy.context.active_object.data.vertices:
            diff_from_ankle = vertex.co - ankle_point

            if diff_from_ankle.z >= 0:
                # Create a deformation zone with linearly decreasing weight from 1 to 0 above the ankle
                DEFORM_ZONE = 0.02
                weight = np.clip(1 - diff_from_ankle.z / DEFORM_ZONE, 0, 1)
            else:
                # Move everyting below the ankle as a solid object
                weight = 1

            foot.add(index=[vertex.index], weight=weight, type='REPLACE')

    def _add_armature(self, ankle_point: mathutils.Vector, foot_name: str):
        """
        The armature is what enables us to rotate the foot around the ankle.
        """
        leg_name = bpy.context.active_object.name

        # Place the armature in the middle of the joint
        LEG_THICKNESS = 0.08
        position = mathutils.Vector((ankle_point.x, ankle_point.y + LEG_THICKNESS / 2, ankle_point.z))

        # Add a bone to the foot, keep track that this is something auto-generated
        old_objects = set(bpy.data.objects)
        bpy.ops.object.armature_add(enter_editmode=False,
                                    align='VIEW',
                                    location=bpy.context.active_object.matrix_world @ position,
                                    rotation=(0, math.radians(-90), 0))
        (list(set(bpy.data.objects) - old_objects))[0][_KEY_MANAGED_ARMATURE] = True

        armature_name = bpy.context.active_object.name
        bpy.data.objects[armature_name].scale = (0.1, 0.1, 0.1)

        # A bone is linked to a vertex group by having the same name
        assert len(bpy.data.objects[armature_name].data.bones) == 1
        bpy.data.objects[armature_name].data.bones[0].name = foot_name

        # Parenting the leg to the bone. Order of selection is imperative
        bpy.ops.object.select_all(action='DESELECT')
        bpy.data.objects[armature_name].select_set(True)
        bpy.data.objects[leg_name].select_set(True)
        bpy.ops.object.parent_set(type='ARMATURE')

        return bpy.data.objects[armature_name]


class ORTHOPEN_OT_leg_prosthesis_generate(bpy.types.Operator):
    """
    Generate a proposal for leg prosthesis cosmetics
    """
    bl_idname = helpers.mangle_operator_name(__qualname__)
    bl_label = "Generate cosmetics"

    max_circumference: bpy.props.FloatProperty(
        name="Calf circumference (max)",
        description="The largest circumference around the calf",
        unit="LENGTH",
        default=0.35)

    height: bpy.props.FloatProperty(name="Cosmetics total height",
                                    description="The extent of the cosmetics, "
                                    "from top to bottom",
                                    unit="LENGTH",
                                    default=0.2)

    clip_position_z: bpy.props.FloatProperty(name="Clip start height",
                                             description="The lowest point of the fastening clips, measured relative to"
                                             " the lowest point of the prosthesis cosmetics",
                                             unit="LENGTH",
                                             default=0.1)

    @ classmethod
    def poll(cls, context):
        return True

    def execute(self, context):

        cosmetics_main, fastening_clip = self._import_from_assets_folder()

        self._adjust_scalings_and_sizes(cosmetics_main, fastening_clip)

        # UI updates
        bpy.ops.object.select_all(action="DESELECT")
        cosmetics_main.select_set(True)
        helpers.set_view_to_xz()

        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def _adjust_scalings_and_sizes(self, cosmetics_main, fastening_clip):

        # Approximate the calf as as perfectly circular, and set the bounding box
        # to a square that would circumvent this circle.
        X_Y_MAX = self.max_circumference / np.pi
        target_size = np.array([X_Y_MAX, X_Y_MAX, self.height])

        # The calf if currently halved long the X-axis, so we have to double the bounding box there
        current_size = helpers.object_size(cosmetics_main) * np.array((2, 1, 1))

        # Scale calf size up to target size
        cosmetics_main.scale = list(np.array(cosmetics_main.scale) * target_size / current_size)

        # Get smallest z-coordinate of the object bounding box
        def get_z_min(object): return (np.amin(np.array(object.bound_box), axis=0))[2] * object.scale[2]

        # Adjust fastening clip bounding box so that its bottom placed in relation to the calf
        # bounding box bottom according to settings.
        # TODO(parlove@paxec.se): Make this more robust, this should fail if parts have non-unitary scaling etc
        fastening_clip.matrix_world.translation += mathutils.Vector((0,
                                                                     0,
                                                                     self.clip_position_z +
                                                                     get_z_min(cosmetics_main) -
                                                                     get_z_min(fastening_clip)))

    def _import_from_assets_folder(self):
        # Import objects from file with assets
        FILE_PATH = Path(__file__).parent.joinpath("assets", "leg_prosthesis.blend")

        with bpy.data.libraries.load(str(FILE_PATH)) as (data_from, data_to):
            # Here .objects are strings, but then the "with" context is exited
            # they will be replaced by corresponding real objects
            data_to.objects = data_from.objects

        # Link all objects to the scene and save a reference to the objects of special interest
        for obj in data_to.objects:
            if obj is not None:
                # Blender might have renamed the objects to "clip_001" etc, hence we use a pattern match instead
                # of direct string comparison
                if "cosmetics_main" in obj.name:
                    cosmetics_main = obj
                if "clip" in obj.name:
                    fastening_clip = obj

                bpy.context.scene.collection.objects.link(obj)

        assert "cosmetics_main" in locals() \
            and "fastening_clip" in locals(), f"Required parts not found in '{FILE_PATH}'"

        return cosmetics_main, fastening_clip


class ORTHOPEN_OT_foot_splint(bpy.types.Operator):
    """
    Generate a foot splint from a scanned foot. Select vertices that should outline the footsplint first.
    """
    bl_idname = helpers.mangle_operator_name(__qualname__)
    bl_label = "Generate"

    @ classmethod
    def poll(cls, context):
        try:
            return bpy.context.active_object.data.total_vert_sel > 2
        except AttributeError:
            return False

    def execute(self, context):

        # We need to switch from 'Edit mode' to 'Object mode' so the selection gets updated
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='EDIT')

        # Extract selected points in the X-Z plane, sorted in in Z direction (from foot to knee)
        mesh = bmesh.from_edit_mesh(bpy.context.active_object.data)
        selected_verts = [(v.co.x, v.co.z) for v in mesh.verts if v.select]
        selected_verts.sort(key=lambda point: point[1])

        # Add vertices that will circumvent the left side of foot (heel and backwards). Now we have
        # polygon that outlines the vertice to be selected
        x_min = -10000
        selected_verts = [(x_min, selected_verts[0][1])] + selected_verts + [(x_min, selected_verts[-1][1])]

        for v in mesh.verts:
            v.select = helpers.inside_polygon(point=(v.co.x, v.co.z), polygon=selected_verts)

        bmesh.update_edit_mesh(bpy.context.active_object.data)
        return {'FINISHED'}


class ORTHOPEN_OT_import_file(bpy.types.Operator, bpy_extras.io_utils.ImportHelper):
    """
    Opens a dialog for importing 3D scans. Use this instead of Blenders
    own import function, as this also does some important work behing the scenes.
    """
    bl_idname = helpers.mangle_operator_name(__qualname__)
    bl_label = "Import 3D scan"
    filter_glob: bpy.props.StringProperty(default='*.stl;*.STL', options={'HIDDEN'})

    def execute(self, context):
        # Import using a file opening dialog
        old_objects = set(context.scene.objects)
        bpy.ops.import_mesh.stl(filepath=self.filepath)
        print(f"Importing '{self.filepath}'")

        # Keep track of what objects we have imported
        imported_objects = set(context.scene.objects) - old_objects
        for object in imported_objects:
            object[_KEY_IMPORTED_SCAN] = True

        # TODO: Rotate the leg
        helpers.set_view_to_xz()
        return {'FINISHED'}


classes = (
    ORTHOPEN_OT_set_foot_pivot,
    ORTHOPEN_OT_permanent_modifiers,
    ORTHOPEN_OT_import_file,
    ORTHOPEN_OT_foot_splint,
    ORTHOPEN_OT_leg_prosthesis_generate,
)
register, unregister = bpy.utils.register_classes_factory(classes)


if __name__ == "__main__":
    register()
