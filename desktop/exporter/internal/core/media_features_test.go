package core

import "testing"

func TestMediaHelpOptionSyntax(t *testing.T) {
	for _, tc := range []struct {
		name, help string
		want       bool
	}{
		{"ffmpeg44", "-vsync              video sync method\n-shortest           finish encoding within shortest input", false},
		{"plain-options", "-fps_mode mode set framerate mode\n-shortest_buf_duration duration maximum buffering", true},
		{"stream-specifier", "-vsync <> deprecated, use -fps_mode\n-shortest_buf_duration maximum buffering\n-fps_mode[:<stream_spec>] set framerate mode", true},
		{"mentioned-not-supported", "-vsync deprecated, use -fps_mode\n-shortest_buf_duration maximum buffering", false},
		{"missing-shortest-buffer", "-fps_mode[:<stream_spec>] set framerate mode", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := supportsMediaPipeline(tc.help); got != tc.want {
				t.Fatalf("supported=%t want=%t", got, tc.want)
			}
		})
	}
}
