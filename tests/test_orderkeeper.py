"""The game mod that keeps the maps block when the mod screen saves. Runs its
Lua against stand-ins for the game's ActiveMods, MapGroups and mod screen.
Needs lupa (pip install lupa); skipped without it."""
import os
import unittest

try:
    import lupa
except ImportError:
    lupa = None

LUA = os.path.join(os.path.dirname(__file__), "..", "gamemod", "mapmover_orderkeeper",
                   "42", "media", "lua", "client", "MapMoverOrderKeeper.lua")

GAME = r'''
local function list(items)
    local l = {items = items or {}}
    function l:size() return #self.items end
    function l:get(i) return self.items[i + 1] end
    function l:isEmpty() return #self.items == 0 end
    function l:add(x) table.insert(self.items, x) end
    function l:clear() self.items = {} end
    function l:contains(x)
        for _, v in ipairs(self.items) do if v == x then return true end end
        return false
    end
    return l
end

folders = {}            -- the map folders the active mods have, game order
written = nil           -- the map order saveModsFile wrote
savefile = nil

function makeMods(order)
    local m = {order = list(order)}
    function m:getMapOrder() return self.order end
    -- What the game does with no shared cell files: drop everything.
    function m:checkMissingMaps() self.order:clear() end
    function m:checkMissingMods() end
    return m
end

MapGroups = {}
function MapGroups.new()
    local g = {}
    function g:createGroups(mods, vanilla) end
    function g:checkMapConflicts() return false end
    function g:getAllMapsInOrder() return list({table.unpack(folders)}) end
    return g
end

local function copy(order)
    local t = {}
    for i = 0, order:size() - 1 do t[#t + 1] = order:get(i) end
    return t
end
function saveModsFile() written = copy(default:getMapOrder()) end
function manipulateSavefile(folder, what) savefile = copy(current:getMapOrder()) end
function require() end
origSave = saveModsFile

fileText = nil          -- default.txt as the game would read it
function getModFileReader(modId, name, create)
    if modId ~= nil or name ~= "default.txt" or not fileText then return nil end
    local lines = {}
    for l in (fileText .. "\n"):gmatch("(.-)\r?\n") do lines[#lines + 1] = l end
    local r, i = {}, 0
    function r:readLine() i = i + 1; return lines[i] end
    function r:close() end
    return r
end
ActiveMods = {}
function ActiveMods.getById(id) return id == "default" and default or current end

ModSelector = {Model = {}}
function ModSelector.Model:getActiveMods()
    if self.loadGameFolder then return current end
    return self.isNewGame and current or default
end
function ModSelector.Model:acceptChanges()
    local activeMods = self:getActiveMods()
    activeMods:checkMissingMods()
    activeMods:checkMissingMaps()
    if self.loadGameFolder then
        self.loadGameFolder = nil
        manipulateSavefile("save", "WriteModsDotTxt")
        return
    end
    if not self.isNewGame then saveModsFile() end
end
'''


@unittest.skipIf(lupa is None, "lupa not installed")
class OrderKeeperTest(unittest.TestCase):
    def load(self, folders, order, file_text=None):
        lua = lupa.LuaRuntime()
        lua.execute(GAME)
        g = lua.globals()
        g.folders = lua.table(*folders)
        g.default = g.makeMods(lua.table(*order))
        g.current = g.makeMods(lua.table(*order))
        g.fileText = file_text
        lua.execute(open(LUA, encoding="utf-8").read())
        return lua, g

    def test_start_up_puts_the_file_order_back(self):
        text = ("VERSION = 1,\r\n\r\nmods\r\n{\r\n    mod = A,\r\n}\r\n\r\nmaps\r\n{\r\n"
                "    map = C,\r\n    map = A,\r\n    map = C,\r\n    map = Gone,\r\n    map = B,\r\n}\r\n")
        _, g = self.load(["A", "B", "C"], [], text)
        self.assertEqual(self.order(g.default), ["C", "A", "B"])

    def test_start_up_leaves_a_kept_order(self):
        _, g = self.load(["A", "B"], ["B", "A"], "maps\n{\n    map = A,\n    map = B,\n}\n")
        self.assertEqual(self.order(g.default), ["B", "A"])

    def test_start_up_without_a_file(self):
        _, g = self.load(["A", "B"], [])
        self.assertEqual(self.order(g.default), [])

    def run_lua(self, folders, order, new_game=False, load_game=False):
        lua, g = self.load(folders, order)
        model = lua.eval("setmetatable({}, {__index = ModSelector.Model})")
        model.isNewGame = new_game
        if load_game:
            model.loadGameFolder = "save"
        g.ModSelector.Model.acceptChanges(model)
        return lua, g

    def order(self, mods):
        o = mods.getMapOrder(mods)
        return [o.get(o, i) for i in range(o.size(o))]

    def test_accept_writes_the_order_back(self):
        _, g = self.run_lua(["A", "B", "C"], ["C", "A", "B"])
        self.assertEqual(list(g.written.values()), ["C", "A", "B"])

    def test_drops_folders_gone_and_adds_new_ones_above_vanilla(self):
        _, g = self.run_lua(["A", "C", "D", "Muldraugh, KY"], ["C", "Gone", "A", "Muldraugh, KY"])
        self.assertEqual(list(g.written.values()), ["C", "A", "D", "Muldraugh, KY"])

    def test_new_game_list_in_memory(self):
        _, g = self.run_lua(["A", "B"], ["B", "A"], new_game=True)
        self.assertIsNone(g.written)
        self.assertEqual(self.order(g.current), ["B", "A"])

    def test_save_game_list(self):
        _, g = self.run_lua(["A", "B"], ["B", "A"], load_game=True)
        self.assertEqual(list(g.savefile.values()), ["B", "A"])

    def test_empty_order_stays_empty(self):
        _, g = self.run_lua(["A", "B"], [])
        self.assertEqual(list(g.written.values()), [])

    def test_globals_put_back(self):
        lua, g = self.run_lua(["A"], ["A"])
        self.assertTrue(lua.eval("saveModsFile == origSave"))
        g.ModSelector.Model.acceptChanges(lua.eval("setmetatable({}, {__index = ModSelector.Model})"))
        self.assertEqual(list(g.written.values()), ["A"])


class InstallKeeperTest(unittest.TestCase):
    def test_copies_in_and_takes_out(self):
        import tempfile
        from mapmover.build import KEEPER_ID, install_keeper
        d = tempfile.mkdtemp()
        dest = os.path.join(d, KEEPER_ID)
        self.assertTrue(install_keeper(d, True, log=lambda *a: None))
        self.assertTrue(os.path.isfile(os.path.join(dest, "42", "mod.info")))
        self.assertTrue(install_keeper(d, True, log=lambda *a: None))   # again, over the old copy
        self.assertEqual(os.listdir(d), [KEEPER_ID])
        self.assertFalse(install_keeper(d, False, log=lambda *a: None))
        self.assertFalse(os.path.exists(dest))


if __name__ == "__main__":
    unittest.main()
